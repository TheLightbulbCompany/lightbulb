# Voice Agents End-State UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the final version of the four voice flows so the founder can test the end state once: (1) in-app voice in front of the super-agent, (2) the phone receptionist, (3) the Meet agent, and (4) the owner's website (onboarding, Home, Calls, Needs you, Agent page, Settings). All four must also work in the Mac app, which is a webview of thelightbulb.company.

**Architecture:** Nothing new at the core. The Router is the realtime voice session in `apps/huddle-agent`, with three entry points: `phone.py`, `meet.py`, and `owner.py` (from #5010). Each entry point does reads and bounded writes through `/internal/voice/*` and hands open-ended work to Hermes Workers with `start_work`. Needs you is the existing Questions system (`routers/questions.py`). Knowledge is the Agent-page document (`GET/PUT /voice/agent`) plus memory. The web is `apps/frontend/src/components/voice/*` under `VoiceShell`. Every current integration stays, as a read source and as Worker tools. Nothing is removed.

**Tech Stack:** FastAPI backend (Python 3.12, DynamoDB), LiveKit Agents worker (Python), OpenAI `gpt-realtime-2.1(-mini)` + `gpt-live-transcribe` (Gemini Live as a fallback flag), Twilio (SIP, SMS), Recall.ai, Next.js 15 + SWR, Tauri Mac app (#5002), Expo (last task).

**Spec:** `docs/superpowers/specs/2026-11-07-voice-agents-design.md` §4–§5 (UX approved on the mockups in `~/Lightbulb/pivot-research-2026-11-07/.superpowers/brainstorm/69159-1794040794/content/`). Master plan phases 3–5: `docs/superpowers/plans/2026-11-07-voice-agents-master-plan.md`. Founder goal: `docs/superpowers/plans/2026-11-08-founder-goal.md`.

**Founder rulings this plan applies (7 Nov evening):**
- "Keep all of our current integration capabilities." Integrations are data and tools the voice agents read and act through. Nothing is removed in this plan.
- "Final version based on our goal and requirements first, so we can test the end-state UX." This plan targets the end state, not increments.
- "We are going all in on only voice agents." Email features are not planned and not tested. Approval examples are voice-driven actions (a booking, an order a Worker places). Wherever the spec mails something (meeting notes to the organizer, the "Email me the notes" checkbox), it is listed under Founder decisions and left as it is in code.

**Founder rulings (7 Nov, late), applied throughout:**
- Meeting notes live in the app only. There is no email: the notes mail is not sent, and the Agent-page checkbox goes (Tasks 22, 29).
- Missed-caller and waitlist texts stay OFF until a consent review (Tasks 9, 29).
- **The voice engine is OpenAI by default for phone, Meet and the in-app orb** (founder, 7 Nov, late; the audit supports it, and we already hold the key and credits). Gemini Live is a fallback flag only.
  - The models are `gpt-realtime-2.1-mini` / `gpt-realtime-2.1` and the transcriber `gpt-live-transcribe`. `gpt-realtime` and `gpt-realtime-mini` retire on 20 Jan 2027, and `gpt-4o-mini-transcribe` on 26 Feb 2027.
  - The one-vendor lane (`feat/voice-openai-engine`) makes the phone and Meet switch. Task 33 covers what that PR leaves.
  - Tasks that relied on Gemini specifics (proactive audio / decide-not-to-respond, the Gemini-TTS greeting, Gemini session resumption) follow the OpenAI engine: the gate owns `response.create`, the opening is the model's own words carrying the three required facts, checked by `DisclosureGuard` (never a pre-rendered, recorded or scripted greeting, founder ruling), and sessions renew before the 60-minute maximum.
- Numbers: an onboarding choice (founder, 7 Nov, late): **keep my number** (forward it: missed and busy by default, or all calls) or **get a new number**. Both get a dedicated number, bought at Go live ($1.15/mo). Purchasing is gated on Twilio KYC; until then the configured prod number serves the founder's test. Porting is a later opt-in (Tasks 11, 30).

**Independent audit folded in:** `~/Lightbulb/super-agent-design/evidence/voice/07-decisions-vs-practice-nov2026.md`. It covers:
- the contact-boundary hole (H → Task 4);
- verbal approval as a voice "yes" plus a one-tap confirm (I → Task 15);
- pass^3 evals, field conditions and real dial-ins (G → Task 39);
- the "what experts do" list (Tasks 34–38).

## Global Constraints

- **The opening is not verbatim** (founder, 7 Nov, late). The first utterance of every call must contain the assistant's name, "AI assistant", and that the call is recorded; the phrasing is the model's own. The check: those three facts are in the opening on ≥ 95% of calls unaided, and on 100% with `DisclosureGuard` (PH:102-163), which re-says what is missing. No exact-greeting, word-for-word, TTS-greeting or pre-rendered-audio requirement anywhere. The Agent page's greeting field becomes a **style guide** (tone, what to mention), not a script.
- Callers (contacts) reach only the bounded set. The boundary is server-side (`voice_phone._allowed`, `effect_check`), never prompt text.
- The Router never blocks on a Worker. Every tool returns in under 1 s, or under 2 s for integration reads (`integration_reads.READ_S`), or says "one moment" and moves on.
- An answer not spoken within 20 s of its question is dropped and posted to chat (Meet) or to the timeline item (app, phone).
- Nothing runs per business while no call or meeting is happening: no new timer, sweeper or always-on process per workspace.
- Approvals: a tap in the member's signed-in app resolves a card (`routers/questions.py` answer path, 10 s undo for anything sent or spent). A member's spoken "yes" in a Meet or on the phone only queues a one-tap confirm pushed to that member's app. Pure voice approval is allowed only for low-risk kinds (an internal task, a reminder), and only from a signed-in Google participant whose email matches a member, or from the Clerk-authenticated in-app orb. A guest's or caller's words never approve (audit I).
- Copy: no markdown in anything spoken or shown in the voice product. The assistant's name comes from `nameOf(config)`. "Lightbulb" is only the product.
- Prices shown: $250/month, 1,500 minutes included, $0.35/min overage (spec §9). There is no charging in this plan; billing is Phase 6.
- No email features. No outbound beyond consented callbacks, booking confirmations, and opt-in day-before reminders. Missed-caller and waitlist texts are off (founder ruling).
- Bookings, moves, cancels and message callback numbers are read back and confirmed by the caller, and the tool enforces it, not the prompt (audit, experts item 1; Task 34).
- Latency target, phone: mouth-to-ear p95 ≤ 1,400 ms on non-tool turns, aiming for 1,115 ms (Twilio, cited in the audit G). Meet keeps its 3 s quiet rule and states its own target in Task 37.
- Eval bar: every scenario passes 3 of 3, and the aggregate is ≥ 90%, including noisy, phone-line and accented runs. "Sellable" also needs 20+ real dial-ins by people other than the builder (Task 39).
- **Golden path, never a hack** (founder, 7 Nov, late): where a vendor documents a way to do something, use it. Every implementer checks vendor docs, official examples and established open source first, and cites them in the PR.
- Ponytail: shortest diff, reuse existing code (`integration_reads`, `questions`, `start_voice_work`, `SettingsPlace`, `HuddleProvider`). Every task is one PR with its test, `/simplify` on the diff, a fresh reviewer PASS posted on the PR, then merge.
- UI tasks (marked **[UI]**) load the `impeccable:impeccable` skill, match the named mockup file, work at 375 px width and in dark mode, and get a Chrome walk on dev plus one in the Mac app, with screenshots in `~/Lightbulb/super-agent-design/evidence/end-state/`.
- Spend: only what the founder goal allows (Twilio ≤ $5/day, voice model (OpenAI or Gemini) ≤ $5/day, Recall ≤ 3 h/day). Anything else is in Founder-only items and waits for a go.

## Review Focus

1. **A business outside New York.** Slots and "inside hours" use `America/New_York` (`voice_phone.py:57`). A Los Angeles salon would book 3 hours off. The test is in Task 2.
2. **Two callers booking overlapping slots at the same moment.** Today only identical starts conflict (`voice_phone.py:359`). The test is in Task 2.
3. **Paused, or not yet live, while a stranger calls.** Today the line still greets, and transfer still dials (`voice_phone.py:80-83`, `:472`). The owner's own test call before Go live must still work. The test is in Task 1.
4. **A guest says "Nova, yes, order them" in a Meet.** The card must stay open and Nova must say it needs a member. The test is in Task 15.
5. **A dev bot or worker reaches prod data, or the reverse.** The env filter in `meetings.py:61-72` must hold for every new webhook and route this plan adds (SMS status, outbound call status). The tests are in Tasks 9 and 10.

---

## Gap analysis (code on origin/main `b1a133a489`, 7 Nov 2026)

Paths: `VP` = `apps/backend/routers/voice_phone.py`, `V` = `apps/backend/routers/voice.py`, `VM` = `apps/backend/routers/voice_meet.py`, `PH`/`MT` = `apps/huddle-agent/src/huddle_agent/{phone,meet}.py`, `FE` = `apps/frontend/src/components/voice/`. "In PR #n" means open and not on main.

**Counts: 57 requirements: 7 built, 37 partial, 13 missing.**

### Phone receptionist (22: 2 built, 13 partial, 7 missing)

| id | Requirement | Status | Evidence | Missing | Task |
|---|---|---|---|---|---|
| P1 | PSTN → LiveKit SIP → worker; line row per number | partial | PH:395-409, agent.py:650-653, VP:65, `lightbulb-stage.ts:60` | SIP dispatch rule is a manual `lk` step (huddle-agent/README.md:117); dev has no number | 11 |
| P2 | Number per business at onboarding | partial | `voice_onboarding.py:187-214`, V:152 | gives out the one env number; a 2nd workspace gets 409 | 11 |
| P3 | Opening carries name, "AI assistant", recorded + disclosure check | built (now scripted TTS on main, `07eb6db029`; the ruling makes it the model's own words, owned by the one-vendor lane) | VP:218-223, PH:102-163, PH:207-240 | — | — |
| P4 | Reads: Knowledge, hours, services, returning caller | partial | VP:139-149, VP:161-166 | static prompt only; caller memory always `[]` (VP:174) | 3 |
| P5 | Bounded writes (availability, book, move, cancel, waitlist, message, transfer, remember) | partial | PH:300-373; VP:341, :383, :402, :426, :444, :472 | no waitlist, no remember | 3 |
| P6 | Contact memory + Forget | partial | VP:122-127, :269-273 | only the name is kept; no Forget route | 3 |
| P7 | Server-side contact boundary from the Agent-page boxes | partial | VP:80-83, VP:209-212, `tasks_repo.py:474-481` | task runs as the owner (`internal_automations.py:250`); sub-tasks drop `phone_contact` (`tasks_repo.py:477`); refusal → "message", not Needs you | 4 |
| P8 | start_work → budgeted task, dedup, Front-desk pre-warm | partial | VP:505, `internal_automations.py:221-265` | no budget, `specialist=None`, no pre-warm | 4 |
| P9 | Call record: transcript, recording, steps, outcome, one sentence, duration | partial | VP:162-198, :250-261, :236-245; PH:525 | no `recordingUrl`; "spam" never set; summary is a `; `-joined list (PH:505) | 5 |
| P10 | Didn't know → Needs you → Knowledge; questions to add | partial | VP:459-465, FE/NeedsYou.tsx:40-46 | card is derived on the frontend; saved under `call.summary`, not the question; needs admin; `toAdd` never written | 6 |
| P11 | Missed hand-off → Needs you | built | VP:481-484, FE/NeedsYou.tsx:85-123 | (frontend-derived card; moves with P10) | 6 |
| P12 | Booking adapters: Google Calendar, Square, Calendly/Zenoti, own calendar, browser write-back | partial | VP:277-432 (own calendar) | `bookingsGo` ignored; no adapters | 7, 8 |
| P13 | SMS confirmation / text back from Nova's number | missing | FE uses `sms:` links | no send path, no 10DLC | 9 |
| P14 | Owner mode by phone (caller ID + PIN) | missing | VP:175 `ownerMode: False` | everything | 12 |
| P15 | Verbal approval by phone | missing | — | everything | 15 |
| P16 | Call back / Text back / Mark as spam routes | partial | V:101 (`/done` only) | Text back, spam | 5, 9 |
| P17 | Minutes metered vs allowance | missing | FE/VoiceHome.tsx:65 client sum | server meter, 80/100% | 13 |
| P18 | Voice choice, Spanish, Ring me | missing | FE/AgentPage.tsx:68-71 UI only | worker ignores `voice`/`spanish` (PH:38); no outbound | 10, 14 |
| P19 | Go live / Pause enforced | partial | VP:80-83 | `live` never read; paused line still greets; transfer ignores pause | 1 |
| P20 | Line-problem card | missing | — | everything | 13 |
| P21 | Forwarding check | missing | FE/Onboarding.tsx:199 placeholder | everything | 10 |
| P22 | Next-time box → Knowledge | partial | FE/VoiceHome.tsx:159-172 | **bug**: saves `{q:""}` and VP:148 drops it | 1 |

### Meet agent (14: 3 built, 9 partial, 2 missing)

| id | Requirement | Status | Evidence | Missing | Task |
|---|---|---|---|---|---|
| M1 | Invite → bot → joins as signed-in Google account | partial | `meeting_invites.py:449,549`, `meeting_bots.py:111-131` | no Recall Google Login Group: waiting room | 16 |
| M2 | Announces itself + rules in chat | built | VM:35-38, :64-80; `meeting_bots.py:122` | two lines, the Recall one with old copy | 17 |
| M3 | Tile = status (Listening·Thinking·Speaking·Drafting·Page ready·Not transcribing X) | partial | MT:458-464, `app/meet-bot/MeetTile.tsx:58-72` | Drafting, Page ready, Not transcribing | 17 |
| M4 | Named only; "stop"; 3 s quiet; 20 s stale → chat | partial | MT:46-47, :83-124, :435-436 | stale only dropped; "stop" not confirmed under server VAD (#5007 note) | 18 |
| M5 | Per-person hearing/attribution | partial (in PR #5007) | main: RMS guess MT:163-170 | merge #5007 | pre |
| M6a | Guest opt-out of transcription | missing (in PR #5009) | — | merge #5009; tile/record of the opt-out | pre, 17 |
| M6b | Guests cannot act | built | VM:83-93 | only host==organizer counts as member | 19 |
| M7 | Reads over Knowledge, past meetings, calls, calendar, Gmail/Drive/Docs, every connected provider, web | partial | VM:161-179, `integration_reads.py:199-209` (9 hand-written of 23 providers in `providers.yaml`) | Knowledge, web search, the rest of providers, month-vs-month | 20 |
| M8 | Office pre-warmed; start_work dedup; drafts → chat link | partial | `meetings.py:192`, VM:103-126, :209-228 | Worker result never comes back to Meet chat; no Office session | 21 |
| M9 | Spend/publish → Needs you; member's "yes" approves | missing | VM:115-116 returns `needs_member`, creates nothing | everything | 15, 19 |
| M10 | Leave only on Recall end; 60 min silence | built | `meetings.py:198-202`, MT:509, :531-537 | check Recall `everyone_left_timeout` (`meeting_bots.py:121`) | 16 |
| M11 | After: asked, follow-ups with live status, decisions kept | partial | VM:236-246, :124-125; `meeting_notes.py:153,237-312` (email: founder decision) | follow-up status frozen at "working"; decisions not written to memory | 21, 22 |
| M12 | Dev/prod isolation | partial | `meetings.py:61-72`, `meeting_bots.py:128` | one account-wide Recall webhook; dev testing is `tests/meet_sim` | 23 |
| M13 | Replay rig is a real gate | partial | `tests/replay/gate.py`, `test_meet.py:182-190` | `ack_within` not asserted; `main()` returns 0 | 23 |

### In-app voice front (5: 0 built, 3 partial, 2 missing)

| id | Requirement | Status | Evidence | Missing | Task |
|---|---|---|---|---|---|
| V1 | Orb → owner-mode Router, Clerk-auth | partial (in PR #5010) | main FE/VoiceHome.tsx:210-256 drives the old Hermes "mouths" (agent.py:663+) | merge #5010 | pre |
| V2 | Talk everywhere incl. Mac app | partial | #5010 rail TalkButton; #5002 restores `apps/desktop` with `NSMicrophoneUsageDescription` (Info.plist:72) and `audio-input` entitlement; app opens `/chat` (tauri.conf.json:9-24) | Mac app lands on `/chat`; WKWebView mic unverified | 24, 31 |
| V3 | Owner tools: reads, bounded writes, start_work, verbal approve | missing on main (partial in #5010: reads + start_work) | — | book/move/cancel/text, approve | 12, 15 |
| V4 | Wakes no per-owner runtime | missing on main (in PR #5010) | #5010 `huddles.py` skips prewake | merge #5010 | pre |
| V5 | Shows state; kept as a "Yours" timeline item | partial | FE/VoiceHome.tsx:44 ("Yours" = phone `ownerMode` only) | no transcript (`record=False`), no timeline item, no thinking/speaking | 24 |

### Owner's website + Mac app (16: 2 built, 12 partial, 2 missing)

| id | Requirement | Status | Evidence | Missing | Task |
|---|---|---|---|---|---|
| W1 | Four places, voice is home | partial | FE/VoiceShell.tsx:19-24 | Settings opens `/chat?panel=settings`; sign-up lands on `/chat` (`sign-up/page.tsx:325`); Mac app opens `/chat` | 25 |
| W2 | Home orb | partial | FE/VoiceHome.tsx:210-257 | see V1/V5 | 24 |
| W3 | One Needs-you line on Home | built | FE/VoiceHome.tsx:72-75 | — | — |
| W4 | Timeline by day + chips | built | FE/VoiceHome.tsx:39-62, :76-92 | ("Yours" empty until 24) | — |
| W5 | Foot: Answered · Booked · Working · Done · N of 1,500 min | partial | FE/VoiceHome.tsx:93-95 | Working/Done, allowance, server minutes | 26 |
| W6 | Row: who · tag · one sentence · duration · row actions | partial | FE/VoiceHome.tsx:117-124 | one sentence (P9), Call back / Text back on the row | 26 |
| W7 | Call pane | partial | FE/VoiceHome.tsx:133-157 | Forget, Mark as spam, recording, real Text back, Next-time bug | 27 |
| W8 | Meeting pane: follow-ups with status, Notes/Transcript/Decisions | partial | FE/VoiceHome.tsx:187-206 | Transcript, Decisions, live status | 27 |
| W9 | "Yours" pane (your talk + its follow-ups) | missing | — | everything (mockup home-v2) | 27 |
| W10 | Needs you: four kinds, action on card, 10 s undo, answer by voice | partial | FE/NeedsYou.tsx:49-123 | Line problem, "Answer & have Nova call back", hold-to-talk answer, undo | 28 |
| W11 | Agent page | partial | FE/AgentPage.tsx:41-148 | Ring me is a `tel:` link (:53); "Join my next meeting"; voice preview; booking-system sign-in | 29 |
| W12 | Onboarding, 5 steps, 15 min, from a phone | partial | FE/Onboarding.tsx; `voice_onboarding.draft` | Google listing, Square/Fresha sign-in, forwarding check, voice preview, Ring me, time zone | 30 |
| W13 | Settings: Billing & minutes, Members, Connections, Approvals, Notifications | partial | `components/settings/*` (AgentGuardrails = Ask/Auto/Never ask) | Billing & minutes for voice (PlanChooser is seat pricing); not inside VoiceShell | 25 |
| W14 | Phone app: four tabs, orb, pushes | missing | mobile tabs = activity, chats, settings, teams | everything | 32 |
| W15 | Mac app | partial | #5002 (see V2) | lands on `/voice`; mic walk | 31 |
| W16 | Calls land live, not on a 20 s poll | partial | FE/data.ts:36 | push | 26 |

**In flight, not planned here (merge first, no overlap):**
- #5007 (Meet per-person hearing).
- #5009 (Meet opt-out, stacked on #5007).
- #5010 (owner voice front).
- #5002 (Mac app source).
- The desktop run-family fix (`fix/task-desktop-run-family`, touches only `desktop_execution.py`).
- The Paperclip exit (`docs/paperclip-exit-plan`, Task 8b). Its Task 3 (native workspace provisioning) is a prerequisite for a brand-new sign-up in Task 30's acceptance, and its Task 5 (Link purchase through the Needs-you Question) shares the approval path with Task 15. Do not edit those files in parallel.
- `feat/voice-openai-engine` (lane-one-vendor) switches phone and Meet to OpenAI (founder-approved). Tools and routes here are engine-agnostic. Worker-prompt tasks (1, 3, 12, 14, 17, 18, 34, 35, 38) rebase onto it once it merges.

---

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `apps/backend/routers/voice_phone.py` | caller tools, line row, call record | 1–6, 12 |
| `apps/backend/core/services/voice_booking.py` (new) | one `Calendar` choice per workspace: own / google / square / browser write-back | 7, 8 |
| `apps/backend/core/services/voice_sms.py` (new) | send SMS from the line's Twilio number; status webhook | 9 |
| `apps/backend/core/services/voice_outbound.py` (new) | outbound SIP calls: ring me, call back, forwarding check | 10 |
| `apps/backend/core/services/voice_usage.py` (new) | minutes per workspace per month; 80/100% line problems | 13 |
| `apps/backend/routers/voice.py` | owner-facing `/voice/*` (timeline, agent, usage, callers, calls/*) | 3, 5, 9, 10, 13, 24 |
| `apps/backend/routers/voice_meet.py` | Meet tools | 15, 17–22 |
| `apps/backend/routers/voice_owner.py` (from #5010) | owner-mode tools | 12, 15, 24 |
| `apps/backend/core/services/voice_approve.py` (new) | verbal approval for all three seats | 15 |
| `apps/backend/core/services/integration_reads.py` | read surface | 20 |
| `apps/huddle-agent/src/huddle_agent/{phone,meet,owner}.py` | tool declarations, prompts, status | 3, 12, 14, 15, 17, 18, 24 |
| `apps/frontend/src/components/voice/*` | the four places | 24–30 |
| `apps/desktop/src-tauri/tauri.conf.json` (from #5002) | Mac app start URL | 31 |
| `apps/mobile/src/app/(app)/(tabs)/*` | phone tabs | 32 |

---

## Scope: ponytail MVP (founder, 7 Nov, late) — this section wins over the task list below

The founder: "Voice agents in front of Hermes for our two use cases, done really well. Limit guards and features. Don't skimp on review or security. Common sense of a senior AWS engineer."

The rule for implementers:
- No pre-recorded audio and no scripts.
- No new tools unless a scenario needs them.
- Prefer the vendor's documented default.
- Review and security checks are unchanged.

**MVP (build these, in this order):**
| # | Task | MVP cut |
|---|---|---|
| 1 | Go live / Pause, Next-time notes | merged (#5014) |
| 20 | Read surface | PASS (#5021) |
| 25 | Voice is home, Settings | merged (#5016) |
| 16 | Signed-in Meet bot | merged (#5017) |
| 4 | Contact boundary | as written |
| 2 | Time zone, no overlaps | as written (#5024) |
| 3 | Caller memory, Forget, remember | **no waitlist tool/route** |
| 6 | Calls → Needs-you Questions → Knowledge | as written |
| 24 | Orb states, Yours rows | as written (#5023) |
| 33 | OpenAI by default | after #5025 (one-vendor lane) |
| 7 | Google Calendar during the call | as written: the owner's calendar is where bookings go |
| 11 | Numbers: keep-and-forward or new | as written, buying behind KYC |
| 10 | **forwarding check only** | `forward_check` purpose and route only; Ring me and call back are later (the owner can simply call the number) |
| 30 | Onboarding | number choice, forwarding check, Go live; **no** Places listing read, **no** Square/Fresha sign-in, **no** voice preview (website read stays) |
| 19 | Members are members; a guest's spend ask → card | as written |
| 21 | Office results back into Meet chat; follow-up status | as written (needed for "draft the post and put the link in chat") |
| 15 | Approvals: **voice "yes" → one-tap confirm card only** | drop the low-risk voice-only path; every seat's "yes" sets `pendingConfirm` and pushes the card |
| 17 | Tile status | Listening · Thinking · Speaking · Drafting · Page ready; one announcement; **no** "Not transcribing" (opt-out parked) |
| 27 | Panes | **Forget** in the call pane + follow-up status in the meeting pane only |
| 28 | Needs you | kind labels, the confirm card ("approved by voice in … · confirm"), Line-problem fix link; **no** hold-to-talk answer, **no** "have Nova call back" |
| 29 | Agent page | greeting style field; remove "Email me the notes"; missed-caller/waitlist texts shown off; **hide** controls that do nothing yet (Voice, Spanish, waitlist checkbox, Square/other systems); no Ring me, no Join next meeting |
| 31 | Mac app opens /voice, mic works | as written |
| 37 | **One basic error metric** | `VoiceCallOutcome{outcome}` plus one alarm on worker/route errors; no p95 latency alarms |
| 23 | **Replay gate fails for real** | the `gate.py` exit code + asserts only; the dev Recall webhook is later |
| 39 | Eval bar | **pass^3 per scenario on the existing harnesses + 20 real dial-ins** before "sellable"; noise, packet-loss, accent and Spanish runs are later |
| #5007 | Per-person hearing / asker | lane PR, merge |

**Later (do not start):**
- 5: recording, one-sentence summary, spam.
- 8: Square/Calendly/Zenoti and browser write-back.
- 9: SMS. It also needs Twilio KYC + 10DLC.
- 10: Ring me and call back.
- 12: owner mode by phone. The orb is owner mode.
- 13: minutes meter and run-out. Billing is Phase 6; the Settings card's fallback sum stays.
- 14: voice choice, Spanish, preview.
- 18: stale answers to chat; the "stop" hardening beyond #5007.
- 22: meeting notes and decisions. Parked, #5018 draft.
- 26: row actions, live push. The 20 s poll stays.
- 32: mobile tabs.
- 34: tool-enforced read-back. The prompt's read-back stays; revisit if the 20 real dial-ins show booking errors.
- 35: spoken progress.
- 36: warm transfer.
- 38: 55-min session renewal. Meetings keep the 90-minute cap.
- #5009: per-guest transcription opt-out. Parked.
- From 15: the low-risk voice-only approvals.
- From 23: the dev Recall webhook.
- From 37: p95 latency alarms.
- From 39: noise, packet-loss and accent runs.

The acceptance checklist below is cut the same way. Steps that need a Later task are skipped in the founder's test:
- phone 2: the text;
- phone 7: the warm part (a cold transfer stays);
- phone 9: owner mode by phone;
- phone 10: spam;
- phone 11: minutes;
- Meet 6: opt-out;
- Meet 8: over an hour;
- the "Answer & have Nova call back" in phone 5 (the answer still goes into Knowledge).

## Order and lanes

**Priority (founder, 7 Nov, late): the live-answer flow first.** The order is:
1. Task 20 (read surface).
2. Task 4 (contact boundary).
3. Task 24 (orb and Yours).
4. The phone flow: 2, 3, 34, 35, 36, 5, 6, 7, 9, 10, 11, 12.
5. Then 15, 19, 21, 26–30.

Task 22 (meeting notes and decisions, #5018) is **parked as a draft**. Nothing depends on it: Task 27's meeting pane shows Decisions only when present, and acceptance step 3.1 ("what did we decide…") leans on memory from earlier meetings, as is.


- **Pre:** #5010 is merged. Merge #5007 (in re-review), then #5009, then #5002. Lane B and Task 31 assume them.
- **First wave (independent, safety and correctness):** 1, 4, 33.
- **Experts-do and eval (audit):** 34 after 3; 35 and 36 after 1; 37 any time; 38 after #5007; 39 after 34–36 (it is the gate for "sellable").
- **Lane A, phone:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8; 9 and 10 after 1; 11 any time (buying stays off until KYC); 12 after #5010; 13 after 6; 14 after 10.
- **Lane B, Meet:** 16, 17, 18, 19, 20, 21, 22, 23 (19 before 15).
- **Shared:** 15 after 6 and 19.
- **Lane C, web:** 25 first; then 24 (after #5010), 26, 27 (after 5, 22, 24), 28 (after 6, 10, 13, 15), 29 (after 10, 14, 16), 30 (after 7, 10, 11), 31 (after 25); 32 last.
- **Gate:** the acceptance checklist below, walked on dev, then the master plan's phase-gate reviews, then prod per the founder goal.

---

### Task 1: The line obeys Go live and Pause; Next-time notes reach the call

**Files:** Modify `apps/backend/routers/voice_phone.py:78-83,139-149,160-200,472-492`. Test `apps/backend/tests/unit/routers/test_voice_phone.py`.

**Interfaces:** Produces `_line_state(cfg: dict, caller: str) -> Literal["live","message_only"]`. A call is `"live"` when `cfg["live"] and not cfg["paused"]`, or when `caller` equals the owner's own number (`cfg["handoff"]["number"]` or `cfg["forwardFrom"]`, both E.164), so the pre-Go-live test call works. The `/calls` start response adds `"mode"`.

- [ ] Write the failing tests:
  - `test_paused_line_takes_messages_only`: book → 403 `not_allowed`, message → 200, transfer → 403.
  - `test_not_live_line_answers_owner_test_call`: with caller = the handoff number, book → 200.
  - `test_next_time_note_reaches_facts`: `_facts({...,"knowledge":[{"q":"","a":"Parking is behind the building"}]})` contains `"Parking is behind the building"`.
- [ ] Run `cd apps/backend && uv run pytest tests/unit/routers/test_voice_phone.py -k "paused or not_live or next_time" --no-cov -q`. Expect 3 failed.
- [ ] Implement. `_allowed` takes the mode. `transfer` checks it. `_facts` renders `q`-less items as `Note: <a>`. The phone prompt (`PH`) says "take a message" when mode is `message_only`.
- [ ] Re-run. Expect 3 passed. Run the whole file. Expect all passing.
- [ ] Commit `fix(voice): the line obeys Go live and Pause; Next-time notes reach calls`.

### Task 2: The business's time zone, and no overlapping bookings

**Files:** Modify `voice_phone.py:57,341-432`, `FE/data.ts` (`timezone?: string` on `AgentConfig`), and `FE/Onboarding.tsx` (step 1 saves `Intl.DateTimeFormat().resolvedOptions().timeZone`). Test `test_voice_phone.py`, `FE/__tests__/onboarding.test.ts`.

**Interfaces:** Produces `cfg["timezone"]` (IANA, default `America/New_York`). Booking writes use a conditional put per 5-minute cell covered by `[start, start + minutes + bufferMin)`, so overlaps fail atomically with 409 `slot_taken`.

- [ ] Write the failing tests:
  - `test_hours_in_business_timezone`: `America/Los_Angeles`, 10:00–19:00. The availability at 09:30 local is not offered, and 18:00 local is.
  - `test_overlapping_start_conflicts`: 60-minute booking at 11:00, then a 30-minute booking at 11:30, gives 409.
  - `onboarding saves timezone`.
- [ ] Run them and expect them to fail. Implement with `zoneinfo.ZoneInfo(cfg.get("timezone") or "America/New_York")` and a DynamoDB `TransactWriteItems` of the cell rows. Run them and expect them to pass.
- [ ] Commit `fix(voice): bookings in the business's time zone; overlaps conflict`.

### Task 3: Caller memory, Forget, waitlist and remember

**Files:** Modify `voice_phone.py` (new routes `POST /internal/voice/calls/{id}/waitlist` and `/remember`; at `end`, write facts), `V` (new route `DELETE /voice/callers/{e164}/memory`), and `PH:300-373` (tools `join_waitlist`, `remember`). Test `test_voice_phone.py`, `test_voice.py`, `apps/huddle-agent/tests/test_phone_prompt.py`.

**Interfaces:**
- `VOICECALLER#<e164>` gains `facts: list[str]` (≤ 10, newest first) and `visits: int`.
- The start response's `callerMemory` is `facts` when `allow.greetByName`, else `[]`.
- `join_waitlist(service: str, day: str, name: str)` writes `VOICEWAIT#<day>#<e164>`, allowed by `allow.waitlist`.
- `remember(fact: str)` adds to the caller's facts (the caller's own preferences only). A fact about the business goes to `cfg["toAdd"]` (Task 6) instead.
- Forget clears `facts` and `name` and returns `{"ok": true}`.

- [ ] Write the failing tests:
  - `test_returning_caller_memory`: remember, end, then start again. `callerMemory == ["Prefers Saturdays"]`.
  - `test_waitlist_respects_allow` (403 when off).
  - `test_forget_caller` (owner route, other workspace → 404).
  - `test_phone_tools_include_waitlist_remember`.
- [ ] Run, implement, run again, commit `feat(voice): caller memory with Forget; waitlist and remember on the phone`.

### Task 4: A call's work runs as the contact, on Front desk, with a budget

**Files:** Modify `apps/backend/core/services/internal_automations.py:204-265`, `apps/backend/core/repos/tasks_repo.py:474-481`, the specialist seed (grep `seed` in `agent_registry`), and `voice_phone.py:494-517`. Test `apps/backend/tests/unit/services/test_voice_start_work.py`.

**Interfaces:**
- `start_voice_work(..., origin: Literal["phone_contact","meeting","owner"], specialist: str | None, budget_s: int = 600)`.
- Phone uses `origin="phone_contact"` and `specialist="front-desk"`.
- Sub-tasks inherit `origin` **structurally** (audit H). The one function every task creation goes through (`tasks_repo.create`, or whatever `start_voice_work` and the Hermes `start_work` effect both call; grep the callers first) copies the parent's `origin` and `from_contact` onto the child. `from_contact` (`tasks_repo.py:475-480`) then reads the task's own field, never only its trigger. No per-caller patch.
- A contact-origin run of `front-desk` gets only the Knowledge read and the booking tools (`voice_booking` free/book/move/cancel, message, `voice_sms.send` to that caller). It gets none of the owner's connectors, MCP servers or browser, except the Task 8 write-back session, which is scoped to the one booking site. This is enforced where the run's tool list is built (grep `allowed_tools` / the Hermes profile toolsets), not in the prompt.
- The seeded specialists per workspace are exactly `front-desk` ("Front desk") and `office` ("Office"), both created if missing.
- When a call connects, `start` calls the existing `voice_calls.prewake_owner`-style warm for `front-desk` only. It is fire-and-forget and adds no new timer.

- [ ] Write the failing tests:
  - `test_phone_work_is_contact_origin`.
  - `test_subtask_inherits_contact_origin` and `test_grandchild_inherits_contact_origin` (two levels deep, created through the Hermes `start_work` effect path, not only `start_voice_work`).
  - `test_contact_run_has_only_knowledge_and_booking_tools`: the tool list has no Gmail, Drive, Stripe or browser, so a caller's "read me the owner's email" cannot even be attempted.
  - `test_contact_task_cannot_pay`: `effect_check` on a `payment` effect with origin `phone_contact` is refused, and a Question is created for the owner.
  - `test_phone_work_has_budget`.
  - `test_seed_front_desk_and_office`.
- [ ] Run, implement, run again, commit `feat(voice): caller work runs as the contact on Front desk with a budget`.

### Task 5: The call record: recording, one sentence, spam

**Files:**
- Modify `PH:500-531`: start LiveKit room-composite audio egress to the existing artifacts S3 bucket at `calls/<ws>/<callId>.ogg`, and write `recordingKey`.
- Modify `voice_phone.py` (`end` stores `recordingKey` and a `summary` sentence).
- Modify `V:_item` (adds a 1-hour presigned `recordingUrl`).
- New route `POST /voice/calls/{id}/spam`.
- Test `test_voice_phone.py`, `test_voice.py`.

**Interfaces:** `summary` is one sentence built from the steps by `summary_sentence(steps: list[dict], booking: str | None) -> str`. Examples: "Gel manicure, Sat 14 Nov 2:30 with Ana · put in Google Calendar · texted confirmation"; "Didn't know the group policy → took a message". Spam sets `outcome="spam"`, `resolvedAt`, and adds the number to `VOICESPAM#<e164>`. A later call from that number is answered, then ended after the greeting, with outcome `spam`.

- [ ] Write the failing tests:
  - `test_summary_sentence_booked` asserts the exact string above.
  - `test_timeline_presigns_recording`.
  - `test_mark_spam_then_next_call_is_spam`.
- [ ] Run, implement, run again, commit `feat(voice): call recording, one-sentence summary, mark as spam`.

### Task 6: "Didn't know" and "wanted a person" become real Needs-you Questions

**Files:**
- Modify `voice_phone.py:444-492`: at `end`, an outcome `needs_you`/`handoff_missed` creates a Question.
- Modify `apps/backend/core/services/questions.py` (kinds `didnt_know`, `handoff`, `line_problem`; options `answer` with text / `call_myself` / `text_back` / `done`).
- Modify `routers/questions.py:194`: answering `didnt_know` with text appends `{q: <the caller's actual question>, a, source: "you answered this in Needs you"}` to the Agent-page document server-side, so no admin save is needed, and removes it from `toAdd`.
- When the caller asked something unknown, `message` with `unknown=true` adds the question to `cfg["toAdd"]`.
- Test `test_voice_phone.py`, `apps/backend/tests/unit/routers/test_questions*.py`.

**Interfaces:** The Question carries `{kind, roomId, caller, question}` and belongs to a per-call task (`start_voice_work`, origin `phone_contact`, title `Answer <caller>: <question>`), so answering resumes that task. The Front-desk Worker then calls back (Task 10) or texts back (Task 9) with the answer. The frontend `callNeedsYou` derivation is deleted in Task 28.

- [ ] Write the failing tests:
  - `test_unknown_creates_question_and_to_add`.
  - `test_answer_writes_knowledge_as_member` (a non-admin member answers, and Knowledge gains the item).
  - `test_handoff_missed_creates_question`.
- [ ] Run, implement, run again, commit `feat(voice): calls put real Questions in Needs you; answers go into Knowledge`.

### Task 7: Google Calendar during the call

**Files:** Create `apps/backend/core/services/voice_booking.py`. Modify `voice_phone.py:341-432` to call it. Test `apps/backend/tests/unit/services/test_voice_booking.py` (mocked `httpx`).

**Interfaces:**
- `async def free(ws, cfg, day: date, minutes: int) -> list[datetime]`
- `async def book(ws, cfg, start: datetime, minutes: int, title: str, caller: str) -> str` (returns the event id)
- `async def move(...)`, `async def cancel(...)`

All dispatch on `cfg["bookingsGo"]`. `own` keeps today's `VOICEBOOK#` rows. `google` uses the workspace's existing Google grant (`access_token.get(ws, "google")`, the same as `integration_reads._calendar`) with `freeBusy` and `events.insert` on the primary calendar. It also writes the `VOICEBOOK#` row as Nova's hold, so the timeline and the meeting reads see it. If the Google call fails or times out after 2 s, the hold stays and a `line_problem` Question is raised ("Google Calendar sign-in expired; 1 booking held").

- [ ] Write the failing tests:
  - `test_google_free_subtracts_busy`.
  - `test_google_book_inserts_and_holds`.
  - `test_google_expired_grant_holds_and_raises_line_problem`.
- [ ] Run, implement, run again, commit `feat(voice): bookings go to Google Calendar during the call`.

### Task 8: Square, Calendly and Zenoti by API; Fresha, GlossGenius, Booksy and Vagaro by browser write-back

**Files:**
- Modify `voice_booking.py` (`square`, `calendly`, `zenoti` branches, the same four functions, using each provider's grant from `providers.yaml`; add the provider entries if a provider is missing).
- `bookingsGo="other"`: `book` writes the hold, then starts a Front-desk task (`start_voice_work`, `origin="phone_contact"`) titled `Write booking to <otherSystem>: <title> <start>`. Its instructions say: sign in with the vaulted login (DynamoDB + KMS, the existing website-login store; grep `website_login`), create the appointment, check it matches the hold, and post screenshots to the call record.
- New owner route `PUT /voice/booking-login` stores `{system, username, password}` encrypted, and is never returned.
- Test `test_voice_booking.py`.

**Interfaces:** `book` returns `"held"` for `other`. The call row's step reads "held in Nova's calendar · writing to Fresha". A write-back conflict raises a `didnt_know`-style Question with the caller's second choice.

- [ ] Write the failing tests:
  - `test_square_book_calls_bookings_api` (mocked).
  - `test_other_system_holds_and_starts_writeback_task`.
  - `test_booking_login_never_returned`.
- [ ] Run, implement, run again, commit `feat(voice): Square/Calendly/Zenoti by API; browser write-back for the rest`.

### Task 9: Texts from Nova's number

**Files:**
- Create `apps/backend/core/services/voice_sms.py`: `async def send(ws, to: str, body: str, kind: Literal["confirmation","reply","reminder"]) -> str`, via Twilio Messages API with the line's number as `From`, using the existing `lightbulb/<env>/twilio_*` secrets.
- New route `POST /webhooks/twilio/sms-status` (signature-checked; an env mismatch is ignored, see Review Focus 5).
- Owner route `POST /voice/calls/{id}/text` `{body}`.
- After `book` (when the caller said yes to a text), send a confirmation.
- A `reminders.dayBefore` reminder is sent by the existing scheduler as a one-shot Temporal timer per booking (no sweeper).
- Test `apps/backend/tests/unit/services/test_voice_sms.py`.

**Interfaces:** Every send writes a step on the call record ("texted confirmation to (415) 555-0133"). `missed_caller` and `waitlist` are not valid kinds: those texts stay off until a consent review (founder ruling), even if the stored config has `reminders.missedCallers` / `reminders.waitlist` true. There is no send unless `kind` is consented: a confirmation needs the caller's yes on the call, a reply goes only to a caller who called, and a reminder needs `reminders.dayBefore` and the caller's yes.

- [ ] Write the failing tests:
  - `test_confirmation_needs_caller_yes`.
  - `test_text_back_route_writes_step`.
  - `test_status_webhook_rejects_bad_signature`.
  - `test_foreign_env_status_ignored`.
  - `test_missed_caller_and_waitlist_texts_never_sent`.
- [ ] Run, implement, run again, commit `feat(voice): confirmations and text back from Nova's number`.

### Task 10: Nova calls out: Ring me, call back with the answer, forwarding check

**Files:**
- Create `apps/backend/core/services/voice_outbound.py`: `async def dial(ws, to: str, purpose: Literal["ring_me","call_back","forward_check"], context: dict) -> str`, through LiveKit `CreateSIPParticipant` on the outbound trunk id in settings `VOICE_OUTBOUND_TRUNK_ID`. It dispatches the phone agent into a `phone-out-` room with metadata `{purpose, context}`.
- Owner routes `POST /voice/ring-me` and `POST /voice/forward-check`.
- When a `didnt_know` Question is answered with text, the Front-desk task calls back via `dial(purpose="call_back", context={answer, question})`.
- `PH` handles `purpose`. `ring_me` opens with "This is Nova. Try booking something, the way a client would." `call_back` opens with the three required facts plus the answer. `forward_check` dials the owner's business line and passes when the call arrives back on the line number within 20 s (the inbound `start` sees `callerNumber == line number` and context `forward_check`); the result is written to `cfg["forwardingOk"]`.
- Test `apps/backend/tests/unit/services/test_voice_outbound.py`, `apps/huddle-agent/tests/test_phone_prompt.py`.

- [ ] Write the failing tests:
  - `test_ring_me_dials_owner_mobile_only` (refuses any number that is not the owner's).
  - `test_call_back_only_to_a_caller_who_called`.
  - `test_forward_check_marks_ok`.
  - `test_outbound_prompt_by_purpose`.
- [ ] Run, implement, run again, commit `feat(voice): ring me, call back with the answer, forwarding check`.

### Task 11: Numbers at onboarding: keep your number (forward it) or get a new one

The founder ruled (7 Nov, late) that this is an onboarding choice: "give them the option to extend one or buy a new one". Research: `~/Lightbulb/super-agent-design/evidence/voice/08-twilio-livekit.md`, the "Numbers at onboarding" section. Routing keys on the dialled number (`PHONE#<e164>` line row), so **every business gets its own number in both paths**. Porting is a later opt-in, not part of this task.

**Paths:**
- **A. Keep my number** (the default). The owner picks a carrier: Verizon, AT&T, T-Mobile, landline, VoIP app (Zoom Phone / OpenPhone / RingCentral / OneTalk), or Google Voice. They then pick a mode:
  - **"Missed and busy calls only"** (default: their phone rings first). Verizon `*71<n>`; AT&T/T-Mobile `**61*<n>#` plus `**67*<n>#`.
  - **"All calls"**. Verizon `*72<n>`; AT&T/T-Mobile `**21*<n>#`; Google Voice and Boost: all calls only.
  - VoIP shows "set 'forward to external number' on busy/no-answer in your admin portal". iPhone gets a note to turn off Live Voicemail. The copy says "check your plan for forwarding charges".
  - The codes live in one table, `FORWARDING_CODES` in `apps/frontend/src/components/voice/forwarding.ts`, with the off codes (`*73`, `##61#`, …).
  - Then the forwarding check (Task 10) verifies that a call reached us.
- **B. Get a new number.** Pick an area code. The bought number becomes their business line, and the page shows it to copy onto their Google Business Profile and website.

**Files:**
- Modify `apps/backend/core/services/voice_onboarding.py:166-214`:
  - `async def reserve(ws, path: Literal["keep","new"], area_code: str | None, carrier: str | None, mode: Literal["missed","all"] | None) -> dict` stores the choice in the Agent-page document as `number = {path, areaCode, carrier, mode}`. Nothing is bought at sign-up.
  - `async def provision_at_go_live(ws) -> str` runs when the document flips `live: true`, and returns the E.164 number.
  - `release_number` releases a bought number; it is called when a workspace stays paused or unused for 30 days, from the existing daily job if one exists, otherwise on the next owner visit. No new timer.
- **Purchase gate:** while `settings.VOICE_BUY_NUMBERS` is false (until Twilio KYC clears), `provision_at_go_live` assigns the configured `settings.VOICE_LINE_NUMBER` (today's single prod number, for the founder's test only), exactly as `assign_number` does now, and a second workspace gets 409 `number_unavailable`. When true, it buys one local number with `AvailablePhoneNumbers/US/Local.json?AreaCode=<a>&VoiceEnabled=true&SmsEnabled=true`, then `IncomingPhoneNumbers.json` with `TrunkSid=<settings.TWILIO_TRUNK_SID>`, and writes the line row. Path A uses the area code of the owner's business line.
- Add `apps/huddle-agent/scripts/phone_dispatch_rule.sh`, an idempotent `lk sip dispatch create|update` from a checked-in JSON, so the dispatch rule is no longer a hand step.
- Routes in `V`: `POST /voice/number` takes `{path, areaCode?, carrier?, mode?}` and calls `reserve`. `PUT /voice/agent` with `live: true` calls `provision_at_go_live` before saving, and fails with 409 when no number can be given.
- Test `apps/backend/tests/unit/services/test_voice_onboarding.py`, `apps/frontend/src/components/voice/__tests__/forwarding.test.ts`.

- [ ] Write the failing tests:
  - `test_reserve_buys_nothing`.
  - `test_go_live_uses_configured_number_when_buying_off`.
  - `test_second_workspace_409_when_buying_off`.
  - `test_go_live_buys_in_area_code_when_on` (mocked Twilio; asserts `AreaCode=610` and `TrunkSid`).
  - `test_keep_path_buys_in_business_line_area_code`.
  - `test_release_returns_number`.
  - `forwarding codes: verizon missed = *71, att all = **21*<n>#, google voice offers all only`.
- [ ] Run them and expect them to fail. Implement. Run them and expect them to pass. Commit `feat(voice): keep-your-number or new-number at onboarding; bought at Go live, gated on KYC`.

### Task 12: Owner mode on the phone, and bounded writes in owner mode

**Files:**
- Modify `voice_phone.py:160-200`: when `callerNumber` is a member's verified mobile (the `handoff.number` today; `cfg["ownerPins"]` maps E.164 to a bcrypt PIN hash) and the PIN spoken in the first turn matches, the response is `ownerMode: true` and the worker switches to the owner tool set from `owner.py` (#5010).
- Add to `owner.py` and `voice_owner.py` the tools `book`, `move`, `cancel` (any customer booking, through `voice_booking`) and `text` (Task 9, `kind="reply"` only to someone who has called).
- Owner route `PUT /voice/owner-pin`.
- Test `test_voice_phone.py`, `apps/backend/tests/unit/routers/test_voice_owner.py`.

- [ ] Write the failing tests:
  - `test_owner_number_without_pin_is_contact`.
  - `test_owner_pin_elevates`.
  - `test_three_wrong_pins_stays_contact`.
  - `test_owner_moves_any_booking_and_texts`.
- [ ] Run, implement, run again, commit `feat(voice): owner mode by phone with a PIN; owner can move and text`.

### Task 13: Minutes, the run-out rule, and Line-problem cards

**Files:**
- Create `apps/backend/core/services/voice_usage.py`: `async def minutes(ws, month: str) -> dict` returns `{"used": int, "included": 1500, "overageRate": 0.35, "runOut": "keep"|"forward"}`. It is summed from call rows' `durationSec` plus meeting minutes, and cached on a `VOICEUSAGE#<yyyy-mm>` counter incremented at call and meeting `end`.
- Route `GET /voice/usage`.
- At `end`, crossing 80% or 100% raises one `line_problem` Question per threshold per month. When `runOut == "forward"` and usage is at 100%, a call is transferred straight to the owner's number.
- Test `apps/backend/tests/unit/services/test_voice_usage.py`.

- [ ] Write the failing tests:
  - `test_usage_counts_calls_and_meetings`.
  - `test_80_percent_raises_once`.
  - `test_runout_forward_transfers`.
- [ ] Run, implement, run again, commit `feat(voice): minutes used, run-out rule, line-problem cards`.

### Task 14: Voice and Spanish are real; voice preview

**Files:**
- Modify `PH:38`, `MT`, `owner.py`: read `cfg["voice"]` and map Warm/Bright/Calm to OpenAI realtime voice names (with a Gemini dict for the fallback flag).
- `cfg["spanish"]` adds "If the caller speaks Spanish, answer in Spanish; the disclosure is said in Spanish too" to the prompt, and the Spanish disclosure words go into `DisclosureGuard`.
- Route `GET /voice/preview?voice=Warm&lang=en` returns a short sample line spoken in that voice ("Hi, I'm Nova…"), for the owner to hear the voice in the browser only. It is never played on a call. Cache it per voice in S3.
- Test `test_phone_prompt.py`, `test_voice.py`.

- [ ] Write the failing tests:
  - `test_voice_setting_maps_to_engine_voice`.
  - `test_spanish_disclosure_accepted`: "Esta llamada se graba" plus "asistente de IA" passes.
  - `test_preview_cached`.
- [ ] Run, implement, run again, commit `feat(voice): the chosen voice, Spanish, and a voice preview`.

### Task 15: Approvals by voice: a "yes" queues a one-tap confirm; pure voice only for low risk

This follows audit I: a voice in a meeting identifies a roster slot, not a person.

**Files:**
- Create `apps/backend/core/services/voice_approve.py`: `async def approve(ws, seat: Literal["app","meet","phone_owner"], speaker: dict, phrase: Literal["yes","no","undo"], hint: str) -> dict`.
  - `speaker` is `{user_id, email, signed_in}`. For Meet it comes from the Recall participant; for the app, from Clerk; for the phone, from owner mode.
  - It picks the open Question the speaker can answer (`questions.can_answer_now`) that best matches `hint` (the most recent when only one is open).
  - It **answers directly** only when one of these holds:
    - `seat == "app"` (the Clerk-authenticated orb);
    - the card's kind is low-risk (`internal_task`, `reminder`) and the speaker is a signed-in Google participant whose email is in `questions.member_emails(ws)`.
  - Otherwise it writes `pendingConfirm = {by, seat, roomId, at}` on the Question and sends the existing Expo/web push to that member: "Approve in one tap: <title> (approved by voice in <meeting>)". It returns `{"confirm_sent": true}`, and Nova says "I've sent it to your phone to confirm."
  - Anything that spends, sends outside the business, or touches customer data always needs the tap.
  - It returns `{"needs_member": true}` for a guest or caller.
- The card (Task 28) shows "Approved by voice in Team meeting · confirm" as the primary button when `pendingConfirm` is set.
- Tools `approve(phrase, hint)` in `meet.py`, `owner.py`, and owner-mode `phone.py`.
- Undo: answers with a sent or spent effect hold for 10 s (`undoUntil`). "Nova, undo", or the card's Undo button, calls `POST /tasks/{t}/questions/{q}/undo` before then.
- Test `apps/backend/tests/unit/services/test_voice_approve.py`.

- [ ] Write the failing tests:
  - `test_meet_member_yes_on_order_sends_confirm_not_answer` (a Worker's supply order, $212: the Question stays open with `pendingConfirm`, and a push is sent).
  - `test_meet_low_risk_from_signed_in_member_answers`.
  - `test_meet_low_risk_from_display_name_only_sends_confirm`.
  - `test_app_orb_yes_answers` (Clerk seat).
  - `test_guest_yes_leaves_card_open`.
  - `test_undo_within_10s`.
  - `test_undo_after_10s_refused`.
- [ ] Run, implement, run again, commit `feat(voice): voice approvals queue a one-tap confirm; voice-only for low risk`.

### Task 16: Nova joins Meet as a signed-in Google account

**Files:** Modify `apps/backend/core/services/meeting_bots.py:111-131`. Add `google_meet.login_required: true` with the Recall Google Login Group id from settings `RECALL_GOOGLE_LOGIN_GROUP_ID` when set, and set `automatic_leave.everyone_left_timeout` to Recall's maximum so only Recall's end ends the meeting. Test `apps/backend/tests/unit/routers/test_meet_bot.py`.

- [ ] Write the failing tests:
  - `test_create_bot_uses_login_group`.
  - `test_create_bot_does_not_leave_on_brief_empty_room`.
- [ ] Run, implement, run again, commit `feat(meet): join as a signed-in Google account`.

### Task 17: One announcement, and the tile is the status

**Files:**
- Modify `meeting_bots.py:122`: drop the old consent line, so the announcement is only VM's.
- Modify VM:35-38 with the exact copy from mockup `meeting-agent.html`: "Hi, I'm Nova, Maya's Nail Studio's AI assistant. I'm listening and I'll only speak when someone says "Nova". Say "Nova, don't transcribe me" and I'll stop for you."
- Modify `MT:458-464`: states `listening | thinking | speaking | drafting | page_ready | not_transcribing:<name>` (`drafting` while a meeting task is working, `page_ready` for 60 s after Task 21 posts a link).
- Modify `apps/frontend/src/app/meet-bot/MeetTile.tsx:58-72` with the labels "Listening", "Thinking", "Speaking", "Drafting…", "Page ready", "Not transcribing Sam".
- Opt-outs (#5009) are written to `META.meeting.optedOut`.
- Test `test_meet.py`, `apps/frontend/src/app/meet-bot/__tests__/MeetTile.test.tsx`.

- [ ] Write the failing tests: `test_single_announcement`, `test_tile_states`, `MeetTile shows Not transcribing <name>`.
- [ ] Run, implement, run again, commit `feat(meet): one announcement; the tile shows what Nova is doing`.

### Task 18: Stale answers go to chat; "Nova, stop" really stops

**Files:** Modify `MT:226-249,435-436`. A reply dropped at 20 s posts its text through `VM` `/chat`. `stop` both calls `session.interrupt()` and clears the output audio buffer (`session.clear_user_turn` / `audio_output.clear_buffer()`, whichever the installed `livekit-agents` exposes), verified in the replay rig. Test `test_meet.py`, `tests/replay/gate.py` scenario `stop_mid_answer`.

- [ ] Write the failing tests: `test_stale_reply_posted_to_chat`, replay `stop_mid_answer` (no audio frames 300 ms after "Nova, stop").
- [ ] Run, implement, run again, commit `fix(meet): stale answers go to chat; stop stops`.

### Task 19: Every workspace member in the meeting is a member; guests' asks become cards

**Files:** Modify `VM:83-93`. A speaker is a member when their Recall participant email (or, failing that, the Google display name matched case-insensitively) is in `questions.member_emails(ws)`. A guest's spend, send or publish ask starts the work with `origin="meeting"`, so the effect gate puts a Needs-you card in front of a member, and Nova says "I'll need Maya's OK for that." Test `test_voice_meet.py`.

- [ ] Write the failing tests:
  - `test_non_host_member_is_member`.
  - `test_guest_order_ask_creates_card` ("order the gel colours", $212).
  - `test_unknown_speaker_is_guest`.
- [ ] Run, implement, run again, commit `feat(meet): members are members; a guest's ask becomes a card`.

### Task 20: The read surface: Knowledge, web, every connected provider, ranges

**Files:**
- Modify `apps/backend/core/services/integration_reads.py:199-240`. Readers not hand-written fall back to the provider's hosted MCP search or list tool (the existing `_mcp`) for every `providers.yaml` entry with an `mcp` url. Providers with neither say "I'll get that" and the tool returns `{"start_work": true}`, so the worker hands it to Office.
- Add a `knowledge` read (the Agent-page Q&A plus instructions, the same `_facts` as the phone) and a `web` read (the existing Parallel provider search, which is already in `providers.yaml`).
- `call_stats` takes `{from, to}` (for "bookings this month versus last").
- The same reads are wired into `owner.py`.
- Test `apps/backend/tests/unit/services/test_integration_reads.py`, `test_voice_meet.py`.

- [ ] Write the failing tests:
  - `test_mcp_fallback_for_unlisted_provider` (e.g. `attio`).
  - `test_no_reader_hands_to_work`.
  - `test_knowledge_read`.
  - `test_web_read_uses_parallel`.
  - `test_call_stats_month_vs_month`.
- [ ] Run, implement, run again, commit `feat(voice): reads over Knowledge, the web and every connected provider`.

### Task 21: Office is warm for the meeting, and its results come back into the Meet

**Files:**
- Modify `meetings.py:192`: warm the `office` specialist at `in_call` (warm only, no new process).
- Modify `internal_automations.start_voice_work` (meeting origin sets `deliverTo = {"meetRoom": room_id}`).
- In the task-completion path (grep `deliverRoomId`), when `deliverTo.meetRoom` is set and the bot is still in the call, post "Draft ready: <title> — <link>" through `VM` `/chat`, and set the follow-up status on `META.meeting.followUps[i]` to `done` / `needs_you` / `working`. When the meeting has ended, update only the status.
- Test `test_voice_meet.py`, `apps/backend/tests/unit/services/test_voice_start_work.py`.

- [ ] Write the failing tests:
  - `test_meeting_task_done_posts_link_in_chat`.
  - `test_followup_status_follows_task`.
  - `test_ended_meeting_updates_status_only`.
- [ ] Run, implement, run again, commit `feat(meet): Office answers back in the Meet; follow-ups track their task`.

### Task 22: Notes and decisions are kept, in the app only

**Files:** Modify `VM:236-246` and `apps/backend/core/services/meeting_notes.py:237-312`. Notes are written to `META.meeting.notes`, and the `Notes: <title>` mail is no longer sent (founder ruling: app only, no email). The function stays; it is not called. At `end`, one cheap-model pass (the existing `cheap_model_ref`) over the transcript writes `META.meeting.decisions: list[str]` and stores each decision in workspace memory with `source="meeting:<roomId>"`, so a later "what did we decide about holiday hours?" finds it via `recall_memory`. Test `test_voice_meet.py`.

- [ ] Write the failing tests: `test_end_writes_decisions_to_meta_and_memory`, `test_recall_finds_last_week_decision`, `test_meeting_notes_not_emailed`.
- [ ] Run, implement, run again, commit `feat(meet): notes and decisions kept in the app, recallable; no notes email`.

### Task 23: Meet dev isolation and a real replay gate

**Files:**
- Modify `meeting_bots.py:128`: set Recall's per-bot `webhook_url` for the environment if Recall accepts it (unverified; check `mcp` docs first). Otherwise prod forwards status events whose `metadata.env != prod` to `api-dev` (signature re-checked).
- Modify `tests/replay/gate.py` so `main()` returns non-zero on any failed `ack_within` or `silent_between`, and `test_meet.py:182-190` asserts both.
- Test `test_meetings_webhook.py`, `test_meet.py`.

- [ ] Write the failing tests: `test_dev_bot_status_reaches_dev`, `test_replay_gate_fails_on_late_ack`.
- [ ] Run, implement, run again, commit `fix(meet): dev bots report to dev; replay gate fails for real`.

### Task 24: [UI] The orb is the Router, shows its state, and every talk is kept as "Yours"

Mockup: `home-v2.html` (the orb and the 8:02 am "You and Lightbulb" row and pane).

**Files:**
- Modify `owner.py` (#5010, merged): set `record=True`. The default `OWNER_ENGINE` is `openai` with `gpt-realtime-2.1-mini` (founder ruling, Task 33); `gemini` stays as a flag. At session end, POST `/internal/voice/owner/{room}/end` with the transcript, the `asked` list and the started task ids.
- Modify `voice_owner.py`: create a hidden room `kind="call"` with `META.call = {ownerMode: true, transcript, summary, followUps}`, so it is a "Yours" row with no frontend change.
- Modify `FE/VoiceHome.tsx:210-257` to use #5010's `useAssistantDm`. The orb states are `connecting | listening | thinking | speaking`, read from the agent participant's `lightbulb.agent.status` attribute. The last assistant line shows as a caption under the orb.
- Test `test_voice_owner.py`, `FE/__tests__/VoiceHome.test.tsx`.

- [ ] Write the failing tests:
  - `test_owner_end_creates_yours_item`.
  - `orb shows thinking then speaking from the agent attribute`.
  - `Yours chip lists the in-app talk`.
- [ ] Load `impeccable:impeccable` and run its critique against `home-v2.html` at 375 px and 1280 px, in light and dark.
- [ ] Run, implement, run again, walk on dev, commit `feat(voice): the orb shows what Nova is doing; talks are kept as Yours`.

### Task 25: [UI] Voice is home; Settings lives in the four places

Mockup: all six (the rail).

**Files:**
- Modify `FE/VoiceShell.tsx:19-24`: Settings goes to `/voice/settings`.
- Create `apps/frontend/src/app/voice/settings/page.tsx`. It renders the existing `SettingsPlace` sections (Members, Connections, Approvals = `AgentGuardrails`, Notifications) plus a new **Billing & minutes** card that reads `/voice/usage` (Task 13). The card shows minutes used of 1,500, $0.35/min after, and the run-out choice between "Keep answering at $0.35/min" and "Send calls to my phone" (the 80% and 100% warnings are Line-problem cards, Task 13).
- Sign-up and sign-in fallback go to `/voice` (`sign-up/[[...sign-up]]/page.tsx:325` and the sign-in page).
- `/chat` stays reachable and loses nothing. It is no longer the landing page.
- Test `FE/__tests__/VoiceShell.test.tsx`, `apps/frontend/src/app/voice/settings/__tests__/page.test.tsx`.

- [ ] Write the failing tests: `Settings rail item opens /voice/settings`, `Billing card shows 38 of 1,500 min`, `sign-up lands on /voice`.
- [ ] Load `impeccable:impeccable`. Run, implement, run again, walk on dev, commit `feat(web): voice is home; Settings with Billing & minutes`.

### Task 26: [UI] Home rows say what Nova did, and calls land live

Mockup: `calls-detail.html` (rows) and `home-v2.html` (foot).

**Files:**
- Modify `FE/VoiceHome.tsx:93-124`. The foot reads "Answered N · Booked N · Working N · Done N · M min of 1,500 used", where Working and Done count follow-ups across today's items and minutes come from `/voice/usage`.
- Rows show the Task 5 sentence. Rows that need it (outcome `message`, `needs_you`, `handoff_missed`) get Call back (`tel:` from the owner's phone) and Text back (Task 9 route, with a one-line inline composer).
- The day header reads "Today — 14 calls · 9 booked · 2 moved · 2 messages · 1 needs you".
- Modify `FE/data.ts:36`: replace the 20 s poll with the existing messaging events socket the chat shell uses (grep `useRoomEvents` / `events` in `components/messaging`). The timeline revalidates on a `room.updated` event for a `kind=call` or meeting room, keeping a 60 s poll as the fallback.
- Test `FE/__tests__/VoiceHome.test.tsx`.

- [ ] Write the failing tests: `foot counts working and done`, `message row has Text back`, `timeline revalidates on room event`.
- [ ] Load `impeccable:impeccable`. Run, implement, run again, walk on dev with a real call landing, commit `feat(web): rows say what Nova did; calls land live`.

### Task 27: [UI] Call, meeting and "Yours" panes complete

Mockup: `calls-detail.html` (call and meeting panes) and `home-v2.html` (Yours pane).

**Files:**
- Modify `FE/VoiceHome.tsx:133-206`.
- Call pane:
  - "What Nova remembers" with **Forget** (Task 3 route).
  - **Mark as spam** (Task 5).
  - Recording player (Task 5 `recordingUrl`).
  - Text back (Task 9).
  - Next time saves `{q:"", a}` (now read by the phone, Task 1).
- Meeting pane: sections Notes, What Nova was asked and did, Follow-ups with live status (Task 21: Done / Working / Needs you), Decisions (Task 22), Transcript (attached `transcript.txt`, collapsed to 6 lines like calls), and "Not transcribed: Sam" when there are opt-outs.
- Yours pane: the transcript as an exchange, plus Follow-ups with status, plus Replay.
- Test `FE/__tests__/VoiceHome.test.tsx`.

- [ ] Write the failing tests: `call pane Forget clears memory`, `meeting pane shows decisions and transcript`, `yours pane lists follow-ups`.
- [ ] Load `impeccable:impeccable`. Run, implement, run again, walk on dev, commit `feat(web): complete call, meeting and Yours panes`.

### Task 28: [UI] Needs you: four kinds, the action on the card, answer by voice, undo

Mockup: `needs-you.html`.

**Files:**
- Modify `FE/NeedsYou.tsx`. Delete `CallCard` and `callNeedsYou`; every card is now a Question (Task 6). The kinds and their buttons:
  - **Nova didn't know**: Answer & have Nova call back (Task 10) / I'll call myself (`tel:`) / Text back (Task 9). It has a hold-to-talk answer button that records one owner utterance through the orb's session, with the transcript dropped into the answer field.
  - **Needs your OK**: Approve / Change / No, then "Undo · 10 s" (Task 15 route).
  - **Caller wanted a person**: Call back / Text back / Done.
  - **Line problem**: the fix link, e.g. Reconnect Google → `/voice/settings#connections`.
- The foot: "Nothing else needs you. 14 calls and 1 meeting handled today — see Home."
- Test `FE/__tests__/NeedsYou.test.tsx`.

- [ ] Write the failing tests: `four card kinds render their actions`, `approve shows undo for 10 s`, `answer and call back posts the answer`.
- [ ] Load `impeccable:impeccable`. Run, implement, run again, walk on dev, commit `feat(web): Needs you with four kinds, voice answers and undo`.

### Task 29: [UI] Agent page: Ring me, Join my next meeting, voice preview, booking-system sign-in

Mockup: `agent-page-v2.html`.

**Files:**
- Modify `FE/AgentPage.tsx:52-56,59-73,108-118,134-139`:
  - Name, greeting & voice: the Greeting field becomes **Greeting style** ("How Nova should open: tone, what to mention"). It is optional and is passed to the model as guidance. Drop the lawful-words check on it in `isLawful` / the `422` in `routers/voice.py:127`, because `DisclosureGuard` guarantees the three facts. The hint says: "Nova always says its name, that it's an AI assistant and that the call is recorded." Test `greeting style is optional and never blocks save`.
  - **Ring me (test call)** in the header (Task 10 `/voice/ring-me`; the status reads "Calling your mobile…").
  - A ▶ preview next to Voice (Task 14).
  - Calendar & booking system: a Square Connect button (the existing `ConnectButton` with provider `square`); for "other", a sign-in form posting to `/voice/booking-login` (Task 8) with the copy "Nova writes each booking there after the call using your login".
  - Number & forwarding: the forwarding status from `cfg.forwardingOk` with "Check forwarding" (Task 10).
  - Meetings: **Join my next meeting**, which posts `/voice/meetings/join-next`. That new route in `V` finds the next Google Calendar event with a Meet link through `integration_reads._calendar` and calls `meeting_bots.create`.
  - Remove the "Email me the notes after" checkbox (notes are in the app only).
  - Reminders: "Text back missed callers" and "Text the waitlist" show as off and disabled with "Off until we finish a consent review" (founder ruling).
- Owner PIN field (Task 12) under Number.
- Test `FE/__tests__/AgentPage.test.tsx`, `test_voice.py::test_join_next_meeting`.

- [ ] Write the failing tests: `Ring me posts ring-me`, `other system shows sign-in form`, `test_join_next_meeting_books_bot`.
- [ ] Load `impeccable:impeccable`. Run, implement, run again, walk on dev, commit `feat(web): Agent page test call, join next meeting, preview, booking sign-in`.

### Task 30: [UI] Onboarding live within 15 minutes, from a phone

Mockup: `onboarding.html`.

**Files:**
- Modify `FE/Onboarding.tsx` and `voice_onboarding.py`:
  - Step 1 reads the Google Business listing too (Places API Text Search plus Details for hours, phone and rating, when `settings.GOOGLE_PLACES_KEY` is set; otherwise only the website) and saves the time zone (Task 2).
  - Step 3 offers Google Calendar / Square / sign Nova in to Fresha-GlossGenius-Booksy-Vagaro (Task 8) / Nova's own calendar. Fresha, GlossGenius and Vagaro say "turn on Google Calendar sync in <system>" (spec §9). Booksy is labelled "beta — Nova keeps its own calendar".
  - Step 4 is **Your number**, with two choices (Task 11):
    - **Keep my number** (default): pick a carrier, pick "Missed and busy calls only" (default) or "All calls", see that carrier's code with the assistant's number filled in, then run the forwarding check (Task 10): "We're ringing your line…", then a tick or the fix.
    - **Get a new number**: pick an area code; "Your new business line is ready at Go live".
    - Before Go live, the assistant's number shown in the code is the one Go live will give (`settings.VOICE_LINE_NUMBER` while buying is off). Porting is not offered here.
  - Step 5 has the voice preview (Task 14), Ring me (Task 10, replacing the `tel:` link at `:241`), and Go live (sets `live`, which the phone now obeys, Task 1).
  - After Go live, the first three calls each push the row (Task 26's socket) with the step-by-step open.
- Test `FE/__tests__/onboarding.test.ts`, `test_voice_onboarding.py::test_draft_merges_places_listing`.

- [ ] Write the failing tests: `step 4 shows forwarding ok`, `step 5 Ring me replaces tel link`, `test_draft_merges_places_listing`.
- [ ] Load `impeccable:impeccable` at 375 px. Run, implement, run again. Time a fresh dev sign-up to Go live on a phone-width Chrome, with a target of 15 minutes or less. Commit `feat(web): onboarding with listing, booking system, forwarding check and Ring me`.

### Task 31: [UI] The Mac app opens voice Home and the mic works

**Files:** Modify `apps/desktop/src-tauri/tauri.conf.json:9-24` (from #5002) so `devUrl`, `frontendDist` and the window `url` are `https://thelightbulb.company/voice`. If the wry WKWebView does not grant `getUserMedia` (check the `on_page_load` / `with_media_permission` hook available in the pinned wry version), add the macOS media-capture permission handler in `apps/desktop/src-tauri/src/main.rs`. Test `apps/desktop/src-tauri` unit test asserting the start URL, then a manual walk: a local `desktop-build` run, open the app, hold the orb, and get an answer.

- [ ] Write the failing test `start_url_is_voice`.
- [ ] Run, implement, run again. Build locally, walk all four flows' web parts in the app, and save screenshots to evidence. Commit `fix(desktop): the Mac app opens voice Home; mic works in the webview`.

### Task 32: [UI] The phone app: four tabs, the orb, pushes (last)

Mockup: `home-v2.html` and `needs-you.html` (phone frames).

**Files:**
- `apps/mobile/src/app/(app)/(tabs)/_layout.tsx`: the tabs are Home · Needs you · Agent · Settings. Keep the existing `chats`, `activity` and `teams` screens reachable from Settings, so nothing is removed.
- Home reuses `providers/live.tsx` for the orb and reads `/voice/timeline`.
- Needs you renders Questions with the same actions.
- Agent is a read-only summary with Pause and Ring me.
- Questions of the four kinds send the existing Expo push (grep `sendPush`) with the card's actions.
- Test `apps/mobile/src/app/(app)/(tabs)/__tests__/layout.test.tsx`.

- [ ] Write the failing tests: `four tabs in order`, `needs-you push has actions`.
- [ ] Load `impeccable:impeccable` (with `expo:expo-native-ui`). Run, implement, run again, then a TestFlight internal build (founder: see below). Commit `feat(mobile): four tabs, orb and Needs-you pushes`.

### Task 33: OpenAI everywhere by default, on the successor models; Gemini a flag

Coordinate with the one-vendor lane (`feat/voice-openai-engine`), which switches phone and Meet. This task covers only what that PR does not; read its diff first and drop anything it already does.

**Files:**
- `apps/huddle-agent/src/huddle_agent/owner.py`: `OWNER_ENGINE` defaults to `openai`, on `gpt-realtime-2.1-mini`.
- `agent.py:549,665`: the legacy Live path's `gpt-realtime` becomes `gpt-realtime-2.1-mini`, and `gpt-4o-mini-transcribe` becomes `gpt-live-transcribe`.
- `meet.py`: the per-person STT from #5007 becomes `gpt-live-transcribe`.
- Grep `src/` for `gpt-realtime"`, `gpt-realtime-mini`, `gpt-4o-mini-transcribe`; change every runtime use.
- Gemini paths stay behind their existing env flags (`PHONE_ENGINE` / `MEET_ENGINE` / `OWNER_ENGINE=gemini`).
- Research first: confirm the model ids and the realtime transcription session shape on OpenAI's docs or deprecations page, and cite them in the PR.
- Test `apps/huddle-agent/tests/test_agent.py::test_no_retiring_openai_models` (none of the three retiring ids appears in `src/`) and `test_default_engines_are_openai`.

- [ ] Write the failing tests. Run them and expect them to fail. Implement. Run the worker suite with `uv run pytest -q -n 2`, expecting only the pre-existing failures. Commit `chore(voice): OpenAI by default on the 2027 successor models; Gemini a flag`.

### Task 34: Read-back is enforced by the tool

**Files:** Modify `voice_phone.py` (book, change, message) and `PH` tools.
- New tool `confirm_details(kind: Literal["book","move","cancel","message"], details: dict)`. It is called after Nova reads the details back and the caller says yes. It stores a `readback` token `{kind, hash(details), at}` on the call.
- `book`, `change` and `message` refuse with 409 `readback_required` unless a token for the same kind and the same details hash (service, start, name; or callback number for messages) is less than 120 s old.
- Owner mode (Task 12) uses the same rule for moves it makes on a customer's booking.
- Test `test_voice_phone.py`.

- [ ] Write the failing tests: `test_book_refused_without_readback`, `test_book_refused_when_details_changed_after_readback`, `test_message_confirms_callback_number`.
- [ ] Run, implement (the prompt names the tool order; the server enforces it), run again, commit `feat(voice): bookings and messages need a confirmed read-back`.

### Task 35: Noise cancellation for phone callers; spoken progress while tools run

**Files:** `PH:500-531`.
- Room input options use `noise_cancellation.BVCTelephony()` for SIP participants. That is the only denoiser in the chain: remove any other. Research first: LiveKit's noise-cancellation docs and its plan availability on our LiveKit Cloud project; cite them.
- While a tool takes longer than 1.2 s, Nova says one short line grounded in the tool and its arguments ("Checking Saturday afternoon for Ana…"), never a generic filler. The silence timer (`076ad3b963`, "ask once, then hang up") pauses while a tool or a hold runs.
- Test `apps/huddle-agent/tests/test_phone_prompt.py` and `test_time_limits.py`.

- [ ] Write the failing tests: `test_sip_participant_gets_bvc_telephony`, `test_progress_line_names_the_tool_args`, `test_silence_timer_paused_during_tool`.
- [ ] Run, implement, run again. Make one scripted noisy call on dev (evidence). Commit `feat(voice): telephony noise cancellation; progress speech; timer pauses for tools`.

### Task 36: Warm transfer to a person

**Files:** `voice_phone.py:472-492`, `PH` transfer tool. Replace the cold SIP transfer with LiveKit's warm-transfer task (research first: LiveKit telephony docs, warm transfer). Nova dials the hand-off number, gives Maya a one-sentence summary ("Jen K., refund for the 3 Oct pedicure, charged twice"), then joins the caller. No answer within 20 s → back to the caller, take a message, and raise a `handoff` Question (Task 6). The same rules apply: opening hours only, and Pause respected (Task 1). Test `test_voice_phone.py`, `test_phone_prompt.py`.

- [ ] Write the failing tests: `test_warm_transfer_summarises_then_bridges`, `test_no_answer_returns_to_caller_and_raises_handoff`.
- [ ] Run, implement, run again. Make one real transfer to the founder's phone on dev, with his go and inside the spend cap. Commit `feat(voice): warm transfer with a summary`.

### Task 37: Per-call observability and alerts

**Files:**
- `PH`, `MT`, `owner.py`: turn on LiveKit Agent observability (research the setting).
- Write per-turn `latencyMs` (user end-of-speech to first agent audio frame, measured in the worker; the PSTN leg is not included and says so) onto the call and meeting records. Write `p50Ms`/`p95Ms` per call at `end`.
- Backend: emit CloudWatch metrics through the existing metrics helper (grep `put_metric`):
  - `VoiceCallOutcome{outcome}`;
  - `VoiceTurnLatencyP95{seat}`.
- Add two alarms on the existing alarm topic in `apps/infra`:
  - phone p95 above 1,400 ms over 1 h;
  - `silent` plus `handoff_missed` above 30% of calls over a day.
- Meet target: reply start ≤ 5 s after the ask ends (the 3 s quiet rule plus 2 s).
- Test `test_voice_phone.py::test_end_writes_latency_percentiles`, `apps/infra/test/*alarms*.test.ts`.

- [ ] Write the failing tests. Run, implement, run again. Commit `feat(voice): per-call latency, outcome metrics and alarms`.

### Task 38: Long meetings reconnect instead of ending

**Files:** `MT:49,509-537`.
- The 90-minute hard cap becomes a planned session renewal at 55 minutes. A new Router session starts with a summary of the meeting so far (the transcript tail plus `asked` plus open follow-ups) and the same gate state; the tile and the bot do not leave.
- OpenAI realtime sessions max out at 60 minutes (audit B), so the renewal is mandatory on the default engine. On the Gemini flag, session resumption may serve instead.
- The 60 min silence rule stays. Phone keeps its 20-minute call limit (`PH:49`).
- Test `test_meet.py::test_session_renews_at_55_min_with_summary`.

- [ ] Write the failing test. Run, implement, run again. Commit `feat(meet): long meetings renew the session with a summary`.

### Task 39: The eval bar: pass^3 per scenario, field conditions, real dial-ins

**Files:**
- `apps/huddle-agent/tests/meet_sim/run.py` and the phone simulation (`origin/test/phone-quality` lane; reuse its harness).
- Scoring: a scenario passes only if it passes 3 of 3 runs (pass^3), and the run also reports the aggregate (must be ≥ 90%).
- Add audio-mode runs with background noise and packet loss (LiveKit `lk agent simulate` options, or our harness mixing a noise bed and dropping 2% of frames), an 8 kHz μ-law phone-line pass, accented English, Spanish, spelled names and phone numbers.
- Report p50/p95 latency per scenario.
- The simulated caller uses a different vendor from the agent where possible.
- Add `docs/superpowers/plans/2026-11-07-end-state-ux.md#sellable` criteria in the evidence card template: pass^3 on all scenarios plus 20 or more real dial-ins on the real number by people who are not the builder, each scored with the same judge.
- Test: `uv run python -m tests.meet_sim.run --check` exits non-zero when any scenario passes fewer than 3 of 3.

- [ ] Write the failing check. Run, implement, run again. Run the full suite once (inside the spend caps) and save the card to evidence. Commit `test(voice): pass^3 bar with noise, phone line, accents; real dial-in criterion`.

---

## End-state acceptance checklist (the founder's one test)

Do it in prod after the gates, on the founder's workspace (Maya's Nail Studio data or his own), in Chrome and then in the Mac app. For each step: **do** → **see**.

### 1. In-app voice (the orb)
1. Open thelightbulb.company (or the Mac app) → you land on Home: orb, then the Needs-you line, then the timeline.
2. Hold the orb and say "What happened today?" → the orb goes Listening, then Thinking, then Speaking. The answer gives counts from today's calls and meetings, and the caption shows the last line.
3. "Move Priya's 11 to 11:30 and text her." → Nova confirms. The booking moves (in Google Calendar if connected). Priya's number gets a text from Nova's number. A **Yours** row appears: "You and Nova · moved Priya, texted".
4. "Draft the holiday-hours post." → "I'm on it." A Working follow-up appears in the Yours pane and turns Done with a link.
5. With a Needs-your-OK card open (an order a Worker wants to place), say "Nova, yes, order them." into the orb (you're signed in) → the card shows Approved · Undo 10 s. Say "Nova, undo" within 10 s → it reopens.
6. Release the orb mid-sentence → nothing is sent half-heard. Press End → the session closes, and the Yours row has the transcript.

### 2. The phone receptionist
1. Call Nova's number from a phone that is not the owner's → in its own words, Nova's opening names itself, says it is an AI assistant, and says the call is recorded. The call record's `disclosure` is `ok`.
2. Book a gel manicure on Saturday afternoon. While it checks, you hear what it is checking ("Checking Saturday afternoon…"), not silence. It offers open slots in the business's time zone, reads back service, day, time and your name, and books only after your yes. Then it asks whether to text the details. Try it from a noisy room too. Say yes → a text arrives from Nova's number.
3. On Home, within seconds and without reloading → a row reads "<name> · new caller · Booked · Gel manicure, Sat … with … · put in Google Calendar · texted confirmation". The pane shows the steps with timestamps, the transcript, the recording, and "What Nova remembers".
4. Call again from the same phone → "returning". Move the booking → Moved row.
5. Ask "Can you do a bridal party of six?" → Nova takes a message. Needs you shows **Nova didn't know** with "Answer & have Nova call back". Type the policy and press it → it goes into Knowledge and Nova calls that phone back with the answer. A third call asking the same question gets it answered.
6. Ask "Can I pay a deposit now?" → Nova says it can't take payments and takes a message (server-side refusal).
7. Ask for a person → Nova rings the hand-off number, tells the person who is calling and why, then connects you (warm transfer). If unanswered → back to Nova, a message, and a **Caller wanted a person** card.
8. Press **Pause** on the Agent page, then call → message only. Resume.
9. Call from the owner's mobile and say the PIN → owner mode. "Move Priya's 11:30 to 12" works.
10. Mark a test call as spam → the next call from that number is a Spam row.
11. Settings › Billing & minutes → minutes used match the calls. A Line-problem card at 80% (tested with a lowered allowance on dev).

### 3. The Meet agent
1. Agent page › **Join my next meeting** (or invite the agents address to a Meet) → Nova joins as a signed-in participant (no waiting room) and posts its announcement in chat once. The tile says Listening.
2. Talk to each other without the name → Nova stays silent.
3. Ask the spec's live asks, one at a time. Each is answered in the meeting:
   - "Nova, what did we decide about holiday hours last week?" (memory, Task 22)
   - "Nova, how many bookings this month versus last?" (Task 20)
   - "Nova, what does our website say about group bookings?" (Knowledge or web)
   - "Nova, pull October's revenue." (Stripe or the connected books)
   - "Nova, draft the post and put the link in chat." → the tile shows Drafting… then Page ready, and the link is in chat.
4. "Nova, stop" mid-answer → it stops at once.
5. A guest (a second Google account) says "Nova, order the gel colours" → Nova: "I'll need <owner>'s OK for that." A Needs-your-OK card appears for the owner. The guest saying "yes" does nothing. The owner saying "Nova, yes, order them" → "I've sent it to your phone to confirm". A push or card arrives saying "approved by voice in Team meeting · confirm", and one tap places it.
6. A guest says "Nova, don't transcribe me" → the tile shows "Not transcribing <name>". Their lines are absent from the transcript.
7. End the meeting → a meeting row appears with "Asked N times · K follow-ups". The pane shows Notes, Decisions, Follow-ups with live status, and the Transcript. No email arrives. Next week, ask about the decision.
8. A meeting longer than an hour → Nova is still there and still answers after the hour.

### Quality bar (before calling it sellable)
- The eval card (Task 39) shows every scenario passing 3/3, including the noisy, phone-line, accented and Spanish runs, with phone p95 latency at or under 1,400 ms.
- 20 or more real dial-ins by people who are not the builder, on the real number, scored by the same judge.
- The dashboard/alarms from Task 37 show the outcome rates for those calls.

### 4. The owner's website (and Mac app)
1. **Onboarding** (a new workspace, on a phone-width browser) → five steps:
   - Step 1: the business, city and mobile. Nova reads the website and the Google listing.
   - Step 2: correct the cards.
   - Step 3: connect Google Calendar or Square, or sign Nova into Fresha.
   - Step 4: choose **Keep my number** (pick your carrier and "missed and busy" or "all calls"; see the code with Nova's number filled in; the forwarding check rings your line and shows a tick) or **Get a new number** (pick an area code).
   - Step 5: name the assistant, optionally write a greeting style, hear the voice preview, press Ring me and book something, then Go live.
   - The whole flow takes 15 minutes or less.
2. **Home** → chips All · Calls · Meetings · Yours · Needs you. The foot reads "Answered · Booked · Working · Done · N of 1,500 min".
3. **Needs you** → four kinds of card, each with its action. The foot says what did not need you.
4. **Agent page** → one long page. Every change saves as you make it. Unchecking "Book appointments" makes the next call take a message instead. Knowledge items show where they came from ("added from a call", "you answered this in Needs you"). There are "questions to add".
5. **Settings** → Billing & minutes, Members, Connections (every current integration still listed and working), Approvals (Ask / Auto / Never ask), Notifications.
6. **Mac app** → opens on voice Home. The orb works with the mic. All of the above works inside it.

---

## Founder-only items (need the founder, an account, or money)

| # | Item | Why | Cost (unverified unless marked) | Blocks |
|---|---|---|---|---|
| 1 | Twilio Trust Hub KYC (number purchase refused 401, evidence `voice/01-phone-dev.md` blocker 2), then a Twilio number for SMS + outbound | today's line is a LiveKit number; SMS and outbound SIP need Twilio | $1.15/number/month (verified in spec §4.1), SIP $0.0034/min | 9, 10, 11 |
| 2 | 10DLC: Lightbulb ISV brand + campaign, a low-volume brand per business | US carriers block unregistered A2P texts | one-time brand + campaign vetting fees plus a monthly campaign fee (check current Twilio pricing) | 9 |
| 3 | Rotate the Twilio API key in the Console (Standard key can't mint keys, `voice/01-phone-dev.md` blocker 3) | the key's secret was displayed once | $0 | 9, 10, 11 |
| 4 | Google: Calendar write scope on the existing OAuth client (verification 4–6 weeks for a calendar-only client, spec §9 Q3; until then the founder's account as a test user) and Google connected on the test workspace | Task 7 books into Google Calendar | $0 | 7, 29 |
| 5 | Google Places API key on project isol8-485204 | onboarding reads the Google listing | per-request Places pricing | 30 (falls back to website only) |
| 6 | Square developer app (OAuth client id/secret into Secrets Manager); test accounts on Calendly / Zenoti if wanted | Task 8 API adapters | $0 (sandbox) | 8 |
| 7 | A Fresha (or GlossGenius) test business login | browser write-back test | $0 trial | 8 |
| 8 | Recall: a signed-in bot Google account (Workspace seat) + a Google Login Group in Recall; a dev webhook or second Recall workspace | waiting room (M1); dev isolation (M12) | one Workspace seat/month | 16, 23 |
| 9 | LiveKit: plan upgrade if a separate dev agent is wanted (1/1 agents, blocker 4) and an outbound SIP trunk | outbound calls; dev worker not on the Mac | LiveKit Ship plan monthly fee | 10, dev walks |
| 10 | Mac app release: Apple signing/notarization in `desktop-build` | a new build with the `/voice` start URL | $0 (existing account) | 31 |
| 11 | TestFlight internal build for the phone app | Task 32 | $0 | 32 |
| 13 | 20+ people who are not the builder to dial the real number (audit G) | the "sellable" bar | $0.0034/min SIP | 39 |
| 12 | OpenAI realtime test minutes (key and credits already held; keep within the ≤ $5/day voice test cap) | voice sessions | ≤ $5/day | all voice walks |

## Founder rulings (recorded 7 Nov, late)

1. **Meeting notes:** in the app only, no email (Tasks 22, 29).
2. **Missed-caller and waitlist texts:** off until a consent review (Tasks 9, 29).
3. **Voice engine:** OpenAI by default for phone, Meet and the orb (`gpt-realtime-2.1(-mini)`, `gpt-live-transcribe`), with Gemini a fallback flag only (founder, 7 Nov, late; the one-vendor lane switches phone and Meet; Task 33 covers the rest).
4. **Numbers:** an onboarding choice, keep-and-forward or new. Each business gets its own number, bought at Go live once KYC clears; until then the configured prod number serves the founder's test. Porting comes later (Tasks 11, 30).
