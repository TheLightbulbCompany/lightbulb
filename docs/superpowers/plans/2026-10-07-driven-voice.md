# Driven voice: the router and the agent decide, the voice speaks

Working plan (delete when shipped). Owner ruling 2026-10-07: one voice stack for every case, on OpenAI's
Realtime model, driven by us. GPT-Live answers whatever it hears and cannot be told to listen without
answering; three brains (GPT-Live, the router, the Hermes agent) were deciding the same things.

## Evidence

- Rig sales call on GPT-Live (three people): answered talk meant for the prospect, took over the call after
  naming her, said a wrong fact from its own head, answered "go ahead" to a stop. Different on each run.
- Spike, `gpt-realtime`, `turn_detection=None`, no microphone: silent unless told; 8 of 8 lines spoken word for
  word (numbers, names, "No."); first audio 0.41-0.66 s after the order (median 0.49 s).
- Prod, 7 days: router p50 0.3 s, p90 0.9 s. Meet speech end to final transcript about 1.3 s (configured).
  So a first acknowledgement word about 2.1-2.7 s after the person stops.

## Who decides what

| Decision | Owner |
|---|---|
| Who a line is for; request, small talk, talk between people, stop, steer | `/route` (exists; `ops.people` added on this branch) |
| The answer | the Hermes run (exists; streamed text, `frames.RunSink`) |
| The words of an acknowledgement or a small-talk reply | the voice model, on the worker's order, from the line's text |
| When to speak, when to stop | the worker: speak on order, stop when someone talks over it or says stop |
| How it sounds | the voice model |

The voice has no ears. It never hears a microphone. Everything it knows arrives as text.

## Phase 1 (this branch): Google Meet, per-person audio

The worker already has a complete "a mouth, not an ear" mode: the mixed-Meet path (`mixed_meet(identity)`),
built for a muted voice fed by the worker's transcript and rig-validated on 10-06 before the duplex change.
Phase 1 puts every Meet voice on that path and swaps the muted GPT-Live session for a driven Realtime session.

### Task A: `agent.py`, the driven session (`_DrivenLive`)

Implements `huddle.Live` on `openai.realtime.RealtimeModel(model="gpt-realtime", voice=..., turn_detection=None)`
in an `AgentSession` with audio input disabled (`RoomOptions(audio_input=False)`), output track
`agent-<agent_id>` as today. Methods:

- `say(text)`: speak `text` word for word. `generate_reply(instructions="Say this line aloud, word for word, and
  nothing else:\n<text>")`. Orders are spoken in order, never overlapped (chain on the previous handle).
- `append_commentary(text, delegation_id=None)`: something to say once in its own words (an acknowledgement
  order, "your earlier task finished: ..."): `generate_reply(instructions=text)`.
- `append_thinking(text, delegation_id=None)`: something to know, never said: added to the chat context as a
  developer message, no response.
- `append_instructions(text)` / `meet_rule(text)`: appended to the session instructions (kept for a reconnect).
- `nudge()`: `generate_reply()` on the context as it stands (the seed already says what to acknowledge).
- `reply_to(text)`: `generate_reply(user_input=text)`.
- `interrupt()`: `session.interrupt(force=True)` and drop the queued orders.
- `mute()`, `hear(since)`, `set_participant(identity)`: no-ops (it has no ears).
- `usage_seconds()`: seconds of the session for billing, as `_Live.usage_seconds` reports them today.
- `aclose()`, and the same `on_agent_state` / `on_agent_said(text, interrupted)` / `on_live_closed` callbacks into
  the huddle as `_start` wires today. `on_agent_said` must carry `interrupted=True` for a cut message.

`start_live` returns a `_DrivenLive` when `in_meet(speaker)`, else today's `_Live` (GPT-Live; Phase 2 moves
in-app Live). Persona for a driven voice: the agent's persona plus "You never decide what to say on your own.
You speak only when you are given an order, and you say only that. Never state a fact that is not in the order."
The gate from PR #3816 (`_heard`, `remember`, `_TrimmedSession.gated`, `hand_over`, `_Live.hear`) is deleted.
Tests in `tests/test_agent.py` with a fake Realtime session: order of speech, word-for-word instruction,
interrupt drops the queue, thinking never speaks, usage.

### Task B: `huddle.py`, every Meet voice is a mouth

- New predicate `mouth(identity)`: the voice for this speaker has no ears. `in_meet(identity)` in Phase 1.
- Every `mixed_meet(...)` use that is about THE VOICE (muted, `MEET_ACK` in the seed, floor lines fed as text,
  `_answer_heard` replay, the holding line, `progress`/`say`/`ended` gates, no `_cut_answer`, the talk-over
  rule for anyone) becomes `mouth(...)`. Uses that are about WHO IS SPEAKING on a mixed track (`_meet_label`,
  `_said_at`, `_talker`, the one-participant-for-everyone follow-up rule) stay `mixed_meet`.
- Run sentences and answers are spoken with `live.say(text)` (word for word) where the mouth path used
  `append_commentary` for them; orders that ask for its own words (acknowledgement) stay `append_commentary`.
- Delete what only served a hearing per-person Meet voice: `MEET_RULES`/`meet_rule` call sites, `_hear`,
  `_span`, `heard_until`/`heard_mark`, the per-person branch of the open nudge, the own-speaker talk-over
  exception (a mouth is stopped by anyone who keeps talking over it, the asker included, and its unsaid part
  goes to the hand), the per-person `_cut_answer` path.
- Keep: `ops.people` closes the floor; the hand (`_news`, `_keep_unsaid`, `unsaid_part` and the bags); trailing
  lines; names anywhere open the exchange; stop commands; no handoff in a meeting.
- Since speech is word for word, `unsaid_part` compares against what was actually said: keep as is.
- Tests: update `tests/test_meeting_mode.py` and `tests/test_meet_exchange.py` to the mouth behaviour; delete
  tests of removed behaviour; every remaining test must pass. Do not weaken an assertion to make it pass.

### Task C: check and ship

- Rig: `natural`, `gap`, `sidetalk`, `sales_call`, read from the meeting transcript. Pass: no reply to talk
  between people, correct facts only, stop is silent, the hand resumes after talk-over, first word within 3 s.
- PR replaces #3816. Worker deploy is its own workflow. `ops.people` needs the backend in prod (prod dispatch
  on the owner's go).
- Billing: a driven session is billed like a GPT-Live one today (session seconds through `usage_seconds`).
  Follow-up: price Realtime by its own rate.

## Phase 2 (next): in-app Live on the same stack, then delete GPT-Live

- `mouth(identity)` becomes true for everyone. The router's `social` reply (small talk) is an order to the
  voice: "<name> said: <line>. Reply briefly as yourself; state no fact about the company."
- In-app speech detection: SEMANTIC_VAD finals took 1.5-5.8 s on the rig; use the Meet setting (400 ms) or
  measure.
- Delete the GPT-Live session, delegation binding (`on_delegation`, `_unbound`), `_cut_answer`,
  `_answer_message`, unheard/resaid, repoint/handoff: about 900 lines the map marked as refereeing only.
- A `progress` frame from the bridge (upstream Hermes `interim_assistant_callback`) instead of inferring
  progress from text before a tool call.

## Not doing

- ElevenLabs or another speech engine (owner: Realtime). A second LLM for small talk (the voice writes it).
- Ears for tone. Pre-rendered acknowledgements.
