# GPT-Live Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Lightbulb's hand-woven realtime "talker" with OpenAI GPT-Live, using client delegation to our DeepSeek-style Router. Ship it on the in-app orb, the Meet agent and the phone receptionist, then re-run the prod e2e tests on all three.

**Architecture:**
- GPT-Live owns the conversation: listening, speaking, turn-taking, interruptions, and what to say while work runs.
- Every substantive request is a client delegation to the existing Router in that surface's room.
- Router replies and Worker thread results go back to GPT-Live as delegation results.
- Our code does no speaking logic. It bridges delegation to the Router, enforces compliance and approvals, and records transcripts.
- Everything above the voice is unchanged: the Router, Workers and threads, the caller-scoped Front desk, cards, and the live transcript in chat.

**Tech Stack:**
- `apps/huddle-agent`: Python with livekit-agents (pinned in `apps/huddle-agent/uv.lock`), the GPT-Live API through OpenAI, and LiveKit SIP for phone.
- `apps/backend`: FastAPI with the existing voice routes.

**Spec:** this plan argues from four sources.
- Founder rulings of 8 Nov 2026: "we should just be building the architecture, not hand weaving what to say when", and "yes, use their voice model with our deepseek router".
- `super-agent-design/evidence/voice/one-brain-plan-2026-11-08.md` (one Router, caller principal).
- `super-agent-design/evidence/voice/voice-architecture-choice-2026-11-08.md` (GPT-Live vs cascaded evidence).
- OpenAI GPT-Live docs: https://developers.openai.com/api/docs/guides/live

## Global Constraints

- **One brain, set by capability, not rules.** GPT-Live has no business data and no tools (client delegation: `GPTLiveModel(delegation="client")`, livekit-plugins-openai 1.8.11 already in uv.lock). Delegation is built into the model and it decides when to delegate; no code or prompt classifies questions. Its prompt holds only who it is, that the Router is its brain, speaking style and compliance.
- **No scripting.** No hard-coded holding lines, fillers, timers, word lists or cut-and-re-say guards in our code. What to say while waiting is GPT-Live's job.
- **Phone compliance.** The first utterance names the assistant and says "AI assistant" and "this call is recorded". This is set in GPT-Live's prompt and verified from the transcript by the existing `DisclosureGuard`, which speaks the exact sentence if one is missing.
- **Approvals are a card tap only.** No voice path approves anything. Meet approvals go to the inviter (#5119).
- **Phone callers are the caller principal.** They get the business profile, booking-only Front desk threads and no owner tokens or memory (#5105, #5108, #5118, #5122). Caller lines go to the call's open thread first (#5122).
- **No feature flags.** One code path, shipped to dev and prod. Nothing under `packages/shared` (that's a prod OTA). Never print secrets. Spend: Twilio ≤ $5/day, Recall ≤ 3 h/day; GPT-Live minutes for tests are named with their cost before a test batch.
- **Delegation is non-blocking.** Interrupting stops speech and never cancels backend work, unless the person asks to stop a thread.

## Review Focus

1. **A second delegation while one is pending** ("also, what's on my calendar?"). Expect both to reach the Router and both answers to be spoken, in order.
2. **GPT-Live paraphrasing adds or changes facts** (PR numbers, prices, times). Expect the spoken answer to name the same items as the Router or Worker message, checked from the transcript.
3. **Meet: unaddressed speech.** Expect silence and no delegation unless the assistant is named.
4. **Phone: the caller talks over the opening.** Expect the disclosure still delivered in full and the caller's line handled afterwards.
5. **A session left open** (orb tab idle, a phone line held). Expect the session to end on silence or hang-up, so billed seconds stay bounded.

---

### Task 0: Spike — GPT-Live contract on our stack (gating)

**Files:**
- Create: `docs/superpowers/specs/2026-11-08-gpt-live-contract.md`
- Create: `apps/huddle-agent/spikes/gpt_live_spike.py` (deleted at the end of Task 5)

**Produces:** the contract later tasks code against. It records:
- the exact model id and how a session connects from a LiveKit worker (plugin, or WebSocket/WebRTC with a sideband);
- the client-delegation event names, request payload and result payload;
- whether results are spoken verbatim or paraphrased;
- interruption behaviour;
- per-second price;
- account access for our OpenAI key, which already backs the `OPENAI_API_KEY` agent secret;
- SIP and phone support through LiveKit;
- measured first-audio latency on one dev orb session.

- [ ] **Step 1:** Confirm our OpenAI account can create a GPT-Live session. One session under 2 minutes, with its cost noted. If access is missing, stop and report; the founder enables it.
- [ ] **Step 2:** From a dev LiveKit worker, run one session that delegates "what have we shipped this week?" to a stub that returns a fixed text. Record each event's name and payload, the spoken output, and its timing.
- [ ] **Step 3:** Write the contract doc with those exact values. Commit it with a message ending in the Co-Authored-By line.
- [ ] **Step 4 (gate):** The founder reads the contract and cost. Tasks 1–6 start only after their go.

### Task 1: `live.py`: the GPT-Live session and the Router bridge

**Files:**
- Create: `apps/huddle-agent/src/huddle_agent/live.py`
- Test: `apps/huddle-agent/tests/test_live.py`

**Interfaces:**
- Consumes: the contract from Task 0, and the existing backend routes:
  - orb: `/internal/voice/owner/{room}/turn` with wake on, and `/events`;
  - Meet: `/internal/voice/meet/{room}/ask` and its events;
  - phone: `/calls/{id}/turn` and `/calls/{id}/events`.
- Produces:
  - `async def start(ctx: JobContext, *, prompt: str, surface: Literal["owner","meet","phone"], post: Callable[[str, list[str]], Awaitable[str]], events: Callable[[], AsyncIterator[dict]]) -> LiveSession`
  - `LiveSession.on_delegate(handler)`, which receives one delegation, calls `post(text, context_turns)` and returns at once.
  - `LiveSession.send_result(delegation_id: str | None, text: str) -> None`, which answers a pending delegation or pushes an unsolicited team message.
  - `LiveSession.close() -> None`, which closes gracefully and collects final usage.

- [ ] **Step 1: Write the failing tests** against a fake GPT-Live transport, using the contract's event shapes.
  - `test_delegation_posts_once_and_returns_immediately`: one delegation event leads to exactly one `post`, and the handler returns before `post` resolves.
  - `test_two_pending_delegations_both_answered_in_order`
  - `test_router_message_becomes_result`: an events item `{kind: "agent", text}` is sent through `send_result` once, by id.
  - `test_interrupt_never_cancels_post`
  - `test_no_scripted_speech`: `live.py` never calls a say or speak API with literal text.
- [ ] **Step 2:** Run `uv run --with pytest-xdist pytest -n 2 tests/test_live.py`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `live.py` per the contract.
- [ ] **Step 4:** Run the tests again. Expected: PASS.
- [ ] **Step 5:** Commit.

### Task 2: In-app orb on GPT-Live

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/owner.py` (replace the realtime talker with `live.start(..., surface="owner")`)
- Modify: `apps/backend/routers/voice_owner.py` only if the contract needs a delegation-id field on `/turn` and `/events`
- Test: `apps/huddle-agent/tests/test_owner.py`

**Interfaces:**
- Consumes: `live.start`, `LiveSession` (Task 1), the `/turn` route (wake on) and `/events` (spoken once by message id).
- Produces: the orb surface on GPT-Live. The live transcript in chat still comes from GPT-Live's input transcription.

- [ ] **Step 1: Write the failing tests.**
  - `test_orb_spoken_line_reaches_router_once`
  - `test_orb_router_reply_sent_as_result`
  - `test_orb_has_no_approve_path`
  - `test_orb_prompt_has_no_business_facts`: the prompt only contains the style, delegation and approval-card text.
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Switch `owner.py` to `live.start`. The prompt says the Router is its brain and holds everything it knows and can do, to speak results naturally without adding facts, and that approvals happen on the card. No list of which questions to delegate.
- [ ] **Step 4:** Run the huddle-agent suite with `-n 2`. Expected: PASS.
- [ ] **Step 5:** Commit.

### Task 3: Meet agent on GPT-Live

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/meet.py`
- Test: `apps/huddle-agent/tests/test_meet.py`

**Interfaces:**
- Consumes: `live.start(..., surface="meet")`, `/meet/ask`, the meeting events, the Recall audio path, the per-speaker transcript, and `meeting_notes` (unchanged).
- Produces: the Meet surface on GPT-Live, which speaks only when named. Thread questions (#5112) are sent as results with their qid, and the next addressed answer within 60 s is posted as the answer (unchanged backend binding).

- [ ] **Step 1: Write the failing tests.**
  - `test_meet_unaddressed_speech_never_delegates` (Review Focus 3)
  - `test_meet_named_question_delegates_with_speaker`
  - `test_meet_link_result_reaches_chat` (unchanged backend, kept)
  - `test_meet_spend_goes_to_inviter_card` (unchanged backend, kept)
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Switch `meet.py` to `live.start`. "Speak only when named" lives in the GPT-Live prompt, plus the existing per-speaker name check on the delegation's source line. That check is a permission gate, not speech scripting, so it stays.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit.

### Task 4: Phone receptionist on GPT-Live over SIP

**Files:**
- Modify: `apps/huddle-agent/src/huddle_agent/phone.py`
- Test: `apps/huddle-agent/tests/test_phone_prompt.py`, `tests/test_phone_live.py`

**Interfaces:**
- Consumes: `live.start(..., surface="phone")`, `/calls/{id}/turn` (the caller principal, which goes to the open thread first), `/calls/{id}/events`, `DisclosureGuard`, the `Ending` class (billing at hang-up), card redaction, transfer and message routes (all kept).
- Produces: the phone surface on GPT-Live.

- [ ] **Step 1: Write the failing tests.**
  - `test_phone_opening_carries_three_facts`, checked from the transcript, with the guard correcting a miss.
  - `test_phone_caller_talks_over_opening` (Review Focus 4)
  - `test_phone_person_and_message_are_delegations_with_kind`: "talk to a person" and "leave a message" go to the backend as delegation kinds. There is no regex routing in our code.
  - `test_phone_hangup_ends_session_and_billing` (Review Focus 5)
- [ ] **Step 2:** Run the tests. Expected: FAIL.
- [ ] **Step 3:** Switch `phone.py` to `live.start`. Keep the SIP participant handling, `Ending`, recording, metering and the run-out forward.
- [ ] **Step 4:** Run the huddle-agent and backend voice suites with `-n 2`. Expected: PASS.
- [ ] **Step 5:** Commit.

### Task 5: Delete the hand-woven talker

**Files:**
- Modify or delete: `apps/huddle-agent/src/huddle_agent/mouth.py`. Remove `Talker`, `HOLD`, `HOLDING`, `SMALL_TALK`, `SAID_TALK`, `REPEAT`, `RESULT_RULE`, `VERBATIM_RULE`, `fixed`, `say_fixed`, `_cut`, `TEAM` and `LINE`, plus the phone's `HOLD_AGAIN`, `OFFER_MESSAGE`, `STILL_THERE`, `CANCEL` and `YES_ONLY`. Keep only what live.py or the backend still imports, such as the transcript helpers.
- Delete: `apps/huddle-agent/spikes/`
- Test: delete the talker-only tests (`tests/test_talker_guard.py`, `tests/test_language_and_lines.py` where they're talker-specific). Keep a test that the deleted names are gone.

- [ ] **Step 1:** Write `test_no_scripted_voice_left`, which greps `src/huddle_agent` for the deleted names. Expected: FAIL.
- [ ] **Step 2:** Delete the code. Grep every import before deleting anything.
- [ ] **Step 3:** Run the huddle-agent and backend voice suites with `-n 2`. Expected: PASS.
- [ ] **Step 4:** Run /ponytail-audit on the diff, then commit.

### Task 6: Ship and run e2e on all three surfaces

**Files:**
- Use: `super-agent-design/evidence/e2e-prod-2026-11-08/phone_harness.py`, the Meet procedure in `meet.md`, and the orb script in `founder-test-script-2026-11-08.md` (part A).
- Create: `super-agent-design/evidence/e2e-prod-2026-11-09/{in-app,meet,phone}.md`

- [ ] **Step 1:** Run a whole-branch review covering /code-review, /security-review and the Review Focus list. Then merge, deploy dev and prod plus the voice agent, and confirm `lk agent status` shows Running.
- [ ] **Step 2:** Phone. Run all 6 steps with `phone_harness.py --script router`. Pass means: the opening has all three facts; booking and moves are heard and correct in the data; the card is refused and redacted; person and message work; billing is real plus 2 s or less.
- [ ] **Step 3:** Meet. Run the same lines in a real Meet (4a–4d, 5). Pass means: silence when unaddressed; real PRs named; the link reaches the chat; the spend card goes to the inviter; action items aren't duplicated.
- [ ] **Step 4:** Orb. Use a test client on dev, and the founder on prod. Pass means "what have we shipped this week?" gives the same answer typed and spoken, with exactly one natural acknowledgement and no repeats.
- [ ] **Step 5:** Write pass or fail, the timings and the costs to the e2e folder, and update the hand-off.
