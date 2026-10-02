# Live and chat routing v2: decide once, re-check before speaking

Status: design, 2026-10-02. Code anchors are on origin/main `35f8ae86e`.
Builds on `2026-10-02-voice-agent-is-the-voice-design.md` ([D]) and `2026-10-02-live-ui-ux-design.md`.

Short forms used for code: `msg` = `apps/backend/core/services/messaging.py`, `jev` = `core/services/jev.py`,
`ow` = `core/services/openclaw_work.py`, `vc` = `core/services/voice_calls.py`, `vr` = `core/services/voice_routing.py`,
`vp` = `core/services/voice_persona.py`, `hd` = `routers/huddles.py`, `wh`/`wa`/`wt`/`wf`/`wg` =
`apps/huddle-agent/src/huddle_agent/{huddle,agent,transcript,frames,gate}.py`, `turn` = `apps/infra/hermes/bridge/turn.py`.

Research is cited by the keys in `.research/multiparty-sources-full-read.json` (gu-survey, gift, penzo, inoue, capa,
time-to-talk, huma, multilight, agreement, eacl-demo, mpgt), plus [Addlesee] =
`.research/addlesee-2023-iwsds-multiparty-data-collection.md`, [Skantze]/[Eshghi] =
`.research/skantze2021-eshghi2016-notes.md`, [Martinez] = `.research/martinez-kennedy-2020-notes.md`, and the
lightbulb.nosync scratch reports [SoS] (speak-or-stay-silent, with prod Jev CloudWatch figures), [V] (option D
verification) and [R] (voice identity). Bench results live under `apps/backend/scripts/speak_gate_bench/`: [MP] =
`results/2026-10-02-multiparty.md` (branch `test/multiparty-routing`, the 356-case `multiparty.jsonl` baseline) and
[AX] = `results/2026-10-02-addressing-experiment.md` with its prototype `addressing.py` (branch
`test/addressing-stage-experiment`). Bench numbers are written chat/voice.

## Owner rulings this design keeps

- Each agent speaks as itself (option D). Voice requests are ordinary Hermes runs on the agent's room session key.
- Nothing from Live is posted to the room timeline.
- Listen modes are chat-only. Any agent in the room can be asked in Live.
- Chat and voice may share routing and rules or not, decided per point on the merits. A shared rule is the same code.
- Hanging up lets runs finish. No hold-to-talk. Paperclip keeps its own keys.
- Native Hermes / LiveKit / OpenAI features first. Fix forward, no feature flags, shortest correct design.

---

## 1. Problem

Jev routes a message **once, at dispatch** (`jev.route`, jev:502; `ow._dispatch_message`, ow:438), and a Live line once,
at its STT final (`vc.route_live_line`, vc:745). The research agrees this is the main structural weakness, and names the
failures we can expect:

| # | Failure | Evidence |
|---|---|---|
| R1 | **A human answers seconds after the question, and the agent posts or speaks a duplicate.** A one-shot dispatch decision cannot see it. | mpgt Table 1 / convo27 ("U1's question was answered by U2 … not dyadic"); huma §3.2 (decision is interruptible until posting); time-to-talk §2 (the scheduler re-decides as each message lands; §7 proposes "generate, then decide whether to send"); multilight Table 3 (next-speaker prediction 49–54% without the next message, 82–85% with it); [Addlesee] condition D |
| R2 | **Implicit handoffs are missed.** Silence-by-default and text-only addressee detection both have low recall. | capa: transcript-only delegate stays silent on 51.4% of its real chances, 2.5% with explicit meeting state; inoue: GPT-4o ~chance on addressee, ~12% recall on implicit addressing, and "always none" scores 80.6% accuracy; eacl-demo: text-only 53% accuracy / 31% recall, 85% / 91% with gaze |
| R3 | **Flat transcript input.** The router sees text, not who-said-what-to-whom. | gift: speaker identification on Len-5 ~62 → 90 P@1 with edge types; penzo: on U6, text-only 0.289 vs 0.454 with structure; gu-survey (a conversation is a reply graph, not a sequence); multilight (speaker-labelled history cuts mistaken identity 25.5% → 2.2%) |
| R4 | **"Mentioned, not addressed"** fires a summon. | [SoS] §2.4 S2 predicted "ask/tell … Name"; shipped wake position 2 (`wt.address`, wt:196-201) fires on "let's ask Forge later". Handled by the addressing stage's `relayed` / `mentioned` labels (§3.1); the position-2 regex is still deleted |
| R5 | **The second overlapping speaker is dropped.** | [Martinez] Table 2 (silently dropping the second speaker lost participants; they used a FIFO queue); shipped: a non-pinned person's unnamed line while a voice is open becomes `append_thinking` only (wh:399-403) and a routed result with a voice open is discarded (wh:479) |
| R6 | **Mid-thought pauses become finals,** so Jev routes half an utterance. | [Skantze] (pauses within a turn are longer than gaps between turns; predict, don't react); [Addlesee] condition B (split utterances); [SoS] §2.2/§5.4 (S3 incomplete-utterance gap). The pinned plugin defaults to `server_vad`, 350 ms silence (livekit-plugins-openai 1.8.3 `stt.py:271-277`, verified). Handled by `semantic_vad` (Live) plus the addressing stage's `complete` label (chat and Live, §3.1): [AX] "Friday, we're trying to" → P(complete) 0.12 → held |
| R7 | **Relayed requests are silenced.** "Leo, Finance has those numbers" or "Bob, ask Finance" opens with a person, so `route_live_line` passes `to_people=True` (vc:771-776) and clamps Jev below 0.7 to none, and the fallback returns None when `people` is set. | mpgt (goals stated on someone else's behalf are a weak spot); inoue Table 1 B; [Addlesee] A and C; [MP] C recall 0.42/0.50. Handled by the addressing stage's `relayed` label plus the `to_people` fix (§3.1): [AX] C agent-named recall 0.22/0.33 → 1.00/1.00 |
| R8 | **Disagreement and group acknowledgement** have no rule. | [Addlesee] F; [Eshghi] (one coalition member's ack grounds the group; "we" requests belong to the group); [Martinez] (reply-from-any) |
| R9 | **We measure accuracy, not recall.** | inoue (always-none 80.6%); multilight App C; capa |

Code-level failures from the shipped map (F-numbers there) that this design also fixes: F4 (cold Jev cards inside the
3 s voice budget), F7 (a voice run queued behind a chat run is marked stale at 120 s and becomes `has_news`), and three
ponytail flags: dead `vr.named_agent`, a second Jev prompt builder `jev.pick_agent`, and the env flag
`LIGHTBULB_PROACTIVE` (wg:14-15), which the no-flags ruling forbids.

## 2. Today (short)

**Chat.** `create_human_message` → `select_dispatch_targets` (rules, ~0 ms: @mention, opening name, DM → `user_request`;
other `active` members → `room_event`; addressed removes ambient) → one `WORK#` row per target → FIFO consumer per
room+agent → for `room_event`, `jev.route` (one shared claimed decision per message, 8 s budget, silence-by-default at
`CONFIDENT_P` 0.7 for agent messages and @handle-to-people messages, fallback chain) → **then** `_steer_live_run`
(ow:569, same agent, same thread or DM) → `create_run` → bridge `_converse` on the room key → reply via
`create_agent_message`, hop cap 2. An ambient (`room_event`) turn may answer `[SILENT]` (turn:2812-2813).

**Live.** Per-person STT (`gpt-4o-mini-transcribe`, realtime) → `wh.on_final_transcript`: stop → name fast path
(`wt.address`, positions 1 and 2, fuzzy/soundex) → barge-in → bare "Lightbulb" → `/route` → `vc.route_voice`
(`mentioned_agent` → `jev.pick_agent` → CEO) → unnamed line with no voice open → `vc.route_live_line` (chat's
`select_dispatch_targets` with every agent forced `active`, then chat's `jev._decide_for_message`, 3 s, no claim row; a lone
speaker's "none" counts as Jev down) → `summon` opens a per-agent GPT-Live session and drives a Hermes run via `/turns`
→ `vc.dispatch_voice_run` on `agent:{agent}:lightbulb:channel:{room}` → bridge takes the same per-(agent, key) lock as chat
→ frames relayed back as commentary. While a voice is open, the pinned speaker is handled natively by GPT-Live; everyone
else's unnamed lines are thinking-only.

The ambient-responder decision is already **one function for both surfaces** (`_decide_for_message`) and the fallback
is shared (`ow._fallback_single`). That is the right shape: a central router plus a separate generator (multilight §4.1:
per-agent "should I speak?" barely beats random). This design keeps it.

## 3. Target architecture

Principle: **route once with every explicit cue we have, then let the newest human evidence revoke or shrink the
answer before it reaches people.** Explicit cues are our substitute for gaze (eacl-demo). Revocation uses the steer we
already have, not a second router call.

```
CHAT                                                   LIVE
human msg                                              human STT final (semantic_vad)
  |                                                       |
select_dispatch_targets  <---- shared rules ---->      wt.address position 1 (0 ms, local)
  + addressing stage (new, shared, §3.1)                  | no name
  |                                                       v
WORK# per target                                       /route -> route_live_line(wake?)
  |                                                       |  addressing stage + select_dispatch_targets (shared)
  | room_event consumer:                                  |  + vr.mentioned_agent as Live "mentions"
  |   1. own live ambient run in this conversation?       |
  |      -> steer "[SILENT] if settled" (NEW ORDER)       |
  |   2. jev.route ------------------------------+        |
  |                                              v        v
  |                         jev._decide_for_message (shared): lines carry `to`,
  |                         state.open lists open runs / the Live floor,
  |                         placement asked only for chat
  v                                                       v
Hermes run on room key                                 floor re-check (0 ms): newer real-speech line
  |  later human lines in the same conversation          from another person -> drop, it routes itself
  |  arrive as steers (ambient: may [SILENT])             |
  v                                                       v
create_agent_message (stores inReplyTo)                summon / or _queue if a voice is open
                                                          | other people's lines -> steer_voice_run (batched 3 s)
                                                          | persona: "already answered -> confirm or add only new"
                                                          v
                                                       Hermes run on room key (same lock), frames -> speech
```

### Components

| What | Where in code | Why (source) | Native feature used |
|---|---|---|---|
| **Steer before routing (chat late answer)** | `ow._dispatch_message`: for a human text message on a `room_event` target, call `_steer_live_run` **before** `jev.route`, still limited to *this* agent and *this* conversation. When the run being steered is ambient (`run.eventKind == "room_event"`, already on the RUN# row), the steer text adds the stand-down sentence. | R1: huma, time-to-talk §7, mpgt, [Eshghi] grounding by proxy | Hermes run steer (`/runs/{id}/steer`), `[SILENT]` → `SILENT` for `room_event` (turn:2813, **no bridge change**) |
| **Open requests in Jev state** | `jev._decide_for_message` / `_state`: `state.open`, ≤5 lines; chat reads `messaging_repo.list_open_runs_for_room`; Live gets `floor` + `voiceRuns` on the `/route` body. One `WHO_INSTRUCTIONS` sentence. | [Martinez] per-request quadruples; [Addlesee] D; huma "continue pending" | — (rows already exist) |
| **`to` edges per line** | `jev._line`; `msg.create_agent_message` stores `inReplyTo` (trigger messageId, already loaded); `vc._transcript_history` fills Live rows' mentions with the shared matcher; worker transcript tags agent lines with the person they answered | R3: gift, penzo, gu-survey, multilight | — |
| **Addressing stage (shared)** | new function in `msg`, beside `select_dispatch_targets`; called from the chat dispatch path and from `vc.route_live_line`. Subsumes the trailing vocative (`msg._addressed_by_name`, msg:2650-2661). §3.1 | R4, R6, R7; inoue Table 3 (end-of-turn vocative); [Skantze] address terms select next speaker; [AX] cued recall 0.62/0.56 → 1.00/1.00 | existing `jev.decide` structured call (same gateway, model, ZDR) |
| **Relayed-request fix** | `vc.route_live_line`: `to_people = bool(people) and not agent_mentions`, where agent mentions now include the stage's `relayed` agents, and the to-people rule is skipped when the stage's `anyone` is high (§3.1); one `WHO_INSTRUCTIONS` sentence on proxy requests | R7: mpgt, inoue, [Addlesee] A/C; [AX] | — |
| **One Live router** | `vc`: delete `route_voice`; `route_live_line(..., wake: bool)`; delete `jev.pick_agent` and `vr.named_agent`; keep `vr.mentioned_agent` as Live's stand-in for stored @mentions | Ponytail flags; penzo (flat-text router is weakest) | — |
| **Drop wake position 2** | `wt.address` (remove the `_VERBS`/`_STRICT_VERBS` loop, wt:90/196-201) | R4: [SoS] §2.4 S2 / §5.1 | — |
| **Pre-speak floor re-check** | `wh._route_and_summon`, before `summon` | R1 Live side: time-to-talk, huma, [Skantze] (first self-selector wins) | — |
| **Second speaker routed, queued** | `wh.on_final_transcript` final `else` branch; `_route_and_summon`'s voice-open guard (wh:479) appends to `_queue` | R5: [Martinez] Table 2 + FIFO queue; [SoS] §5.2.3; [Addlesee] D | — |
| **Late lines steer the floor-holder's run** | `wh._flush_lines` (every `THINKING_EVERY` = 3 s) → existing `steer_voice_run` (vc:681) | R1 Live side: [Martinez] match out-of-turn replies to open requests; [Eshghi] | Hermes steer |
| **Persona: already answered** | `vp._AGENT` (vp:64-74), one sentence | [Addlesee] D/F | GPT-Live's own judgement over `append_thinking` |
| **Disagreement / "we" / group ack** | `turn._observe_prompt` (read by chat room turns and voice runs alike); `jev.NONE_CRITERION` carve-out | R8: [Addlesee] F, [Eshghi], [Martinez] reply-from-any, [Skantze] §8.3 | — |
| **Semantic end of turn** | `wa.entrypoint` (wa:205): `openai.STT(..., turn_detection={"type": "semantic_vad", "eagerness": "auto"})` | R6: [Skantze]; [SoS] §2.2/§5.4 | OpenAI realtime transcription `semantic_vad`; the pinned plugin 1.8.3 accepts a `turn_detection` dict and validates it against the OpenAI SDK union (`stt.py:111-115, 196, 271`) |
| **Live skips placement** | `_decide_for_message(..., placement=False)` from `route_live_line` | Live never reads placement (jev:462 asks it today) | — |
| **Card prewarm at join** | `hd.join`, beside `prewake_owner` | F4 | — |
| **Queued voice runs don't go stale** | `wf.Runs.running()`: a run with `queued=True` uses the 1,800 s chat run budget instead of `STALE_TURN_SECONDS` 120 | F7; [Skantze] holding through a processing delay | the bridge's existing `run_queued` frame |
| **Delete `LIGHTBULB_PROACTIVE`** | `wg` (gate.py:14-15) and its caller | no-flags ruling | — |
| **Recall metrics + bench** | worker, `ow`, `msg`; `apps/backend/scripts/speak_gate_bench` | R9 | — |

### 3.1 Addressing stage (shared)

One function in `msg`, beside `select_dispatch_targets`, run on every human message before targets are picked. The chat
dispatch path and `vc.route_live_line` both call it, so chat and Live share the code, not a copy. It replaces the regex
cues (trailing vocative, position 2) with one labelled decision and feeds the `to_people` rule. Prototype: [AX] `addressing.py`.

1. **Candidates (deterministic, ~0 ms).** Every agent name and person name / first name anywhere in the text. Chat:
   word boundary, plus @handles. Live: the worker's fuzzy/soundex matcher from `wt` (the prototype imports
   `huddle_agent.transcript`'s `_fuzzy`; production reuses that matcher, it does not copy it). No candidate → the stage
   is skipped and routing is today's path. [AX]: 223 of 349 human channel messages named nobody.
2. **One structured call, only if there is at least one candidate.** The existing `jev.decide` (same AI Gateway, model and
   zero-data-retention options; choices and probabilities only, no free text) labels each candidate
   `addressed | relayed | mentioned | not_a_name`, plus two booleans: `complete` (is the utterance finished) and `anyone`
   (is something asked of anyone). Timeout 2 s.
3. **Fast path, no call.** The text opens with exactly one agent name, no other candidate is present, ≥ 4 words, it ends
   in terminal punctuation (chat) or is a Live final, and the name is neither a weekday nor shared with a person → that
   agent is addressed. [AX]: 8 messages took it.
4. **Deterministic fallback** on error or timeout: candidate agent names are addressed except weekday names; a name shared
   with a person goes to the person; a plain person name is `mentioned`. [AX] run (c), the fallback alone: cued recall
   0.92/0.88 and C 1.00/1.00, but overall false-speak 0.08/0.08 and the common-word false-speaks come back ("Pulse
   check", "Forge ahead"). A safe fallback, not a substitute.
5. **Decision.**
   - Addressed and relayed agents **all** respond, the same semantics as several @mentions today. Jev's one-responder
     rule applies only to the ambient pick. [AX]: first-named-only costs the two-agent rows, chat recall 0.92 → 0.90.
   - `anyone` high (P ≥ 0.5) → the to-people silence rule does not apply. This fixes a regression the stage itself
     introduced: [AX] D05 "what's our MRR, and Sam where did you put the deck?" labels Sam addressed and silences
     Finance (A19 "for Jordan: when does the Acme contract renew?" has the same shape). Unmeasured until the bench re-runs.
   - A weekday name followed by a scheduling statement ("Friday, we ship the beta") → `not_a_name`, a deterministic
     veto. This is domain knowledge (our agents' names collide with weekdays), not bench tuning; [AX] did not add it and
     measured R03 at addressed 0.92 without it.
   - `complete` < 0.5: Live holds the line until that speaker's next final; chat routes anyway, and the continuation
     reaches the run as a steer (§4.1).
   - Otherwise the shared Jev path runs unchanged, with the stage's labels feeding `to` on the line (§5).
6. **Cost.** [AX] (b): one call on 117 of 349 messages, p50 330 ms, p95 ~500 ms (475–518 ms across runs), one 2 s timeout
   in 117 calls (fell back). The fast path skipped 8 calls.

Measured effect ([AX], chat/voice, baseline [MP] → stage): cued recall 0.62/0.56 → 1.00/1.00; cued false-speak 0.50/0.75 →
0.17/0.25; C agent-named (relay / mid-sentence name) recall 0.22/0.33 → 1.00/1.00; overall recall 0.83/0.82 →
0.92/0.91; overall false-speak flat at 0.05/0.07. No-name rows never reach the stage and do not move (implicit recall
0.89/0.89).

Known remaining misses ([AX] remaining-misses table): "Forge ahead, team" (R06, addressed 0.71–0.75; not vetoed, watched
by `live.route.unwanted`); the D05/A19 shape until the `anyone` rule is measured; and the D-class rows that name nobody or
only a person (D01 second open goal, D03 re-ask after a human answered, D12 partial answer), which the stage cannot see.
Rewording `NONE_CRITERION` so a re-asked question counts as new did **not** move D03 ([AX] run (d): none at 0.95, no
regressions either), so wording alone is not the fix. D-class is **pending a separate experiment** on timestamps /
open-request state.

## 4. Re-check before speaking, in detail

There are two seams where a decision can be revoked: before the agent's run starts or finishes (chat and Live), and
before audio starts (Live only). We use the cheapest lever at each. Neither seam adds an LLM call (the one added call is
the addressing stage, §3.1, before routing).

### 4.1 Chat: steer the agent's own ambient run before Jev runs

Today, a human line in a conversation where agent X has a live run reaches X's consumer, which **first** asks Jev
(ow:491). If Jev picks someone else or "none", the consumer returns at ow:563 **without steering**, so X's run never sees
the line and posts its answer anyway. That is R1 (Bob answers "$42k" 3 s after Alice's ambient "what's our MRR?";
Finance still posts $42k).

Change, in `_dispatch_message`, for `event_kind == "room_event"` and no follow-up:

1. Call `_steer_live_run(meta, member, message, ...)` **before** `jev.route`. Its scope stays exactly as shipped: this
   agent only, runs in `dispatched`/`started`, same thread or DM (ow:707-716). No widening to other agents: dispatch is
   one consumer per `WORK#` target, and an agent that is not a target of the new message has no consumer to steer from.
2. If the steered run is ambient (`run["eventKind"] == "room_event"`), the steer text is:
   > `{name}: {text}` — said since you were asked. If this already answers or settles what you were going to say, reply
   > exactly [SILENT]. If it adds to it, answer only what is still open. If it is unrelated, carry on.

   Addressed runs keep today's text (`{name}: {text}`). The bridge already maps `[SILENT]` to `SILENT` for `room_event`
   runs (turn:2813), so **nothing changes in the image**, and the agent's Hermes session records that it stood down — no
   "ghost" reply it believes it posted (the problem with a backend-side post filter).
3. If the steer was taken, the consumer returns True, as today. The other consumers of the same message still call
   `jev.route`. Jev now sees X's open run in `state.open` (section 5), so it is told not to add a second responder. If the
   shared decision picks X anyway, nobody new runs, which is the right outcome: X is handling it.

> **Correction (2026-10-02, PR #3599):** the "no bridge change" claim above is wrong for a steer that lands after the
> agent's last model call: `_converse` answered it in a second pass and joined the replies, so a stand-down posted
> `"<answer>\n\n[SILENT]"`. PR #3599 drops the whole reply on a `[SILENT]` last pass; the image must be rolled first.

Why addressed runs are excluded from the stand-down: the person chose that agent, and a hedged human answer must not
silence it (huma/mpgt test idea "hedged human answer → Finance still answers"). The asker's own "never mind" already
reaches an addressed run as a steer (shipped).

Known limits, accepted:
- A new message that @mentions another agent removes every `room_event` target (msg:2710-2713), so X has no consumer and
  is not steered. X's reply may then duplicate. Measured by `messaging.reply.after_human_answer` (section 10).
- A top-level ambient run has `threadRootId` None, so any top-level human line in that channel steers into it while it is
  live. Unrelated lines arrive labelled "if it is unrelated, carry on". Watch `messaging.run.steered{kind=ambient}`.
- A run that already finished generating cannot be stopped. Steer reaches it at the next Hermes step.

### 4.2 Live: drop a routed summon superseded by a newer line (0 ms)

In `wh._route_and_summon`, after `/route` returns and before `summon`: if another person's `real_speech` final arrived
after `heard_at`, return and emit `live.route.superseded`. Exempt the same speaker continuing (that is a split utterance,
[Addlesee] B; semantic_vad should merge most of these anyway). The newer line routes itself with the earlier one in the
transcript, so Jev decides on the latest state ("Bob already said $42k" → none). This sits beside the existing "a voice
opened meanwhile" check.

### 4.3 Live: lines said during an in-flight voice run reach that run

While the floor-holder has a running driven run, other people's `real_speech` lines are already batched every 3 s for
`append_thinking` (wh:399-403). Also send that batch to `steer_voice_run` (vc:681) as
"Said in the room since you were asked (may or may not concern you): …". Hermes, with the full room session, decides
whether the request is settled. This is the Live analogue of 4.1, using the same native steer. Voice runs cap at 4 steps
(`CALL_RUN_LIMITS`, turn:283), which bounds the cost.

### 4.4 Live: delivery manners

One sentence in `vp._AGENT`: "If someone already answered while you were working, don't repeat it: confirm in a few
words or add only what's new. If what they said is wrong, say so kindly and give the right answer." GPT-Live already
holds those lines as thinking; this is native judgement at the last moment before audio.

## 5. Routing input structure for Jev

One prompt builder, `jev._decide_for_message`, serves chat and Live. Changes are to its input, not its question.

**Per line (`_line`).** Each history row becomes
`{author, kind: human|agent, to: [names], self: true if the candidate wrote it, text}`:
- **Human chat line:** `to` = names of its `user_request` targets (`dispatchTargets`) plus mentioned people. Both stored.
- **Agent chat line:** `to` = author of `inReplyTo`. `create_agent_message` already loads the trigger
  (msg:3668); it stores its messageId as `inReplyTo`. Thread replies also get the thread root's author when `inReplyTo`
  is outside the window.
- **Live line:** `vc._transcript_history` (vc:729) runs the addressing stage's deterministic candidate detection (§3.1
  step 1, no call per history row) and `vr.mentioned_agent` on each row; the current line's `to` comes from the stage's
  labels. The worker tags agent lines with the person the voice was pinned to
  (`Forge → Leo: …`), and `_transcript_history` parses the arrow.
- Window stays 30 rows / 24k chars (jev:49-52): penzo and gift both find long context hurts addressee recognition.
- Cost: ~+10% input tokens.

**Open requests (`state.open`).** Up to 5 lines: `Finance is answering Alice's "what's our MRR?" (main, 40 s ago)`.
- Chat: `messaging_repo.list_open_runs_for_room` (an owner-partition Query filtered by room in code, ~10–30 ms; the steer
  path already runs it).
- Live: the `/route` body carries `floor = {agent, speaker}` and `voiceRuns = [{agent, request}]` from worker memory.
- New `WHO_INSTRUCTIONS` sentence: "A teammate already working on a request owns its follow-ups; don't add a second
  responder to it."

**Proxy requests.** New `WHO_INSTRUCTIONS` sentence: "A request made on someone's behalf ('Alice needs the deploy logs',
'Leo, Finance has those numbers') is answered by the teammate named or best placed, speaking to the person who needs it."

**Disagreement.** `NONE_CRITERION`'s "something a colleague has already answered" gains "…unless a teammate knows that
answer is wrong".

**Placement.** `placement=False` from Live: the question is not asked, fewer output tokens.

**Silence threshold.** `CONFIDENT_P` 0.7 stays shared and uncalibrated ([SoS] §1.9.7). The `to_people` signal differs
per surface on the merits: chat uses an @handle opening (`jev.addressed_to_people`, jev:394, which already returns False
when any agent is mentioned); Live hears first names, not handles, and now also returns False when any agent is named
anywhere (R7). On both surfaces the rule is skipped when the addressing stage's `anyone` is high (§3.1). Live keeps its
one extra rule: "none" stands only with 2+ humans in the call (capa's 51.4% miss rate is the
cost of silence to a lone speaker who can only be talking to the agents).

## 6. Live turn-taking changes

Per decision point, shared or separate, and why:

| Point | Decision | Why |
|---|---|---|
| Opening name (position 1) | **Separate** fast path `wt.address`, unchanged | 0 ms local, STT-error tolerance (fuzzy/soundex), name lists. A network hop can't give that. |
| Mid-line "ask/tell/get/have Name" (position 2) | **Deleted.** Such lines go to `/route`, where the addressing stage labels the name `relayed` or `mentioned` | Being talked about is not being talked to ([SoS] §2.4 S2). Cost: the stage call (p50 330 ms, p95 ~500 ms, [AX]) on lines that name someone; the name-switch latency gate is on position 1, untouched. |
| Any name not at position 1 (trailing vocative, relay, mid-sentence name, several names) | **Shared** addressing stage (§3.1), in `msg`, reached through `/route` | inoue Table 3; [AX] C 0.22/0.33 → 1.00/1.00. Subsumes the trailing vocative. Addressed and relayed agents all respond. |
| "Lightbulb, …" | `route_live_line(..., wake=True)`: the worker strips the wake word first (so "Lightbulb, Forge, check CI" opens with Forge and is addressed at 0 ms in `select_dispatch_targets`); `wake` forces `to_people=False` and treats "none" as Jev down | A wake word means "someone answer". Note `humans=1` alone is **not** equivalent: "Lightbulb, Leo needs the deck" would open with a person and hit the `if people: return None` leg (vc:787). Deletes `route_voice` and `pick_agent`. |
| Ambient responder | **Shared** `_decide_for_message`; Live keeps a 3 s budget, no claim row, no retry | A person is waiting, and there is one caller per line. |
| Second speaker while a voice is open | `real_speech`, unnamed, not a barge-in, not the pinned speaker → still appended as thinking **and** routed via `/route` with `floor`. A picked agent goes on `_queue` with `(agent, speaker, line)`; when the floor settles (`_settled`, wh:967) it is summoned with that speaker pinned. If it is the floor-holder, that is a re-point to the second speaker. "none" → thinking only. | [Martinez] FIFO queue; [Addlesee] D. "none" stands here because 2+ people are present by definition. |
| Turn completion | `semantic_vad`, eagerness `auto`. Interims still drive barge-in. | [Skantze]; R6. Fallback if the latency gate fails: eagerness `high`. |
| Agent hop cap | n/a | Only human finals route in Live; agents never trigger routing. |
| Stop | unchanged | — |

Not changed: GPT-Live owns turn-taking for its pinned speaker ([D] §9); barge-in rules; floor timers (idle 20 s, bind
10 s, handoff 8 s); proactive news limits (90 s gap, 10/hour). Only the env kill switch goes.

## 7. State: what is stored where

Nothing new is stored except one optional attribute.

| State | Lives in | New? |
|---|---|---|
| Open chat requests (agent, trigger, thread, status, eventKind) | `OWNER#/RUN#` rows in `lightbulb-{env}-messaging` | no (read by Jev now, as well as the steer path) |
| Who-to-whom | MSG `mentions`, `dispatchTargets[].eventKind`, **`inReplyTo`** on agent messages | `inReplyTo` only |
| Shared Jev decision | `ROOM#/DECISION#<msg>` (7-day TTL) | no |
| Steered messages per run | `add_steered_message` on the RUN# row | no |
| Live floor, queue, voice runs, transcript | worker memory (`_voice`, `_queue`, `_runs`, `Transcript`) | no; `_queue` entries gain the speaker and line |
| Voice runs | `OWNER#/VOICERUN#` (2 h TTL), not in the RUN# ledger | no |
| Agent's memory of the room (chat + voice turns, observed rows) | Hermes room session in the profile's `state.db` (S3) | no |
| Jev card cache | in-process, `.revision`-keyed (jev:294) | no; prewarmed at join |

## 8. Latency and cost budget

### Latency

| Step | Today | After |
|---|---|---|
| Chat rules (C1) | ~0 ms | same |
| Chat ambient steer check | after Jev | before Jev: one RUN# Query ~10–30 ms; a steer invoke 100–300 ms (10 s cap); the steered consumer skips Jev entirely |
| Chat Jev | p50 417 ms, p90 822 ms | + `to` fields and `state.open` (~+10% input tokens), < +50 ms |
| Live STT final | 350 ms silence + transcription | `semantic_vad`: clean endings about the same; incomplete-sounding lines wait longer (est. +0.2–0.8 s, up to ~4 s; unmeasured). Barge-in unchanged (interims). |
| Live position-1 name | ~0 ms | same |
| Live position-2 / unnamed / bare Lightbulb | position-2 0 ms; unnamed + Lightbulb ~0.4–0.8 s | all go through Jev: p50 417 / p90 822 ms, 3 s budget; warm cards keep it inside budget; placement dropped |
| Addressing stage (chat and Live, only when the text names someone) | — | 0 ms with no candidate or on the fast path; otherwise one `jev.decide` call, p50 330 ms, p95 ~500 ms, 2 s timeout then fallback ([AX]: 117 calls on 349 messages, 1 timeout) |
| Floor re-check | — | 0 ms |
| Second-speaker route | dropped | ~0.4–0.8 s, off the floor-holder's critical path (result queues) |
| GPT-Live first audio | ~2–3 s ([V] §2) | same |
| `/turns` invoke | 18 s/attempt, 75 s worker retry | same |
| Queued voice run | stale at 120 s → `has_news` | waits up to 1,800 s, then spoken |

Gate: [D]'s proposed latency gate (name/switch p50 within 500 ms of baseline, p90 ≤ 4 s) is the acceptance bar for the
worker changes.

### Cost

- Jev on Vercel AI Gateway: ~$0.000046/call (`scripts/speak_gate_bench/results/2026-10-01.md`: $0.0745 for 1,615 calls).
  Added calls: position-2 lines and second-speaker lines in Live, tens per call-hour. Well under $0.01 per active
  room-hour. The bare-Lightbulb path still makes one call.
- Addressing stage: one structured `jev.decide` call per message that names someone (117 of 349 in [AX]), same price
  class as a Jev route call. Messages that name nobody, and fast-path messages, cost nothing.
- No other new LLM call in chat. A stood-down ambient run saves its posted answer.
- `semantic_vad`: same model, same billed audio.
- Card prewarm: N Paperclip GETs + up to 4N S3 reads per join, which the first `/route` would have paid.
- Late-line steers into voice runs: extra Hermes input tokens, bounded by 3 s batching and 4 steps.
- DynamoDB: one RUN# Query per Jev decision; one attribute on agent MSGs.
- No new table, queue, Lambda, service or flag.

## 9. What we are NOT building, and why

One correction to the earlier draft, which said no new LLM call is added on any path: the addressing stage (§3.1) adds
**one bounded structured call, only on messages that name someone**, justified by the measured gain ([AX]: cued recall
0.62/0.56 → 1.00/1.00, false-speak flat). Every exclusion below still stands.

- **An open-goal ledger or LLM dialogue-state tracker.** *Decided: not built.* The D-class experiment ([DX] =
  `results/2026-10-02-dclass-experiment.md`, branch `test/dclass-experiment`) measured both candidate signals on top of
  the addressing stage: relative timestamps in Jev's lines bought nothing (D 0.67 → 0.67); an open-request `state.open`
  block, built from hand labels (its upper bound), fixed one row (D12 partial answer, Finance 0.30 → 0.77) and left D01
  and D03 unmoved (none ≥ 0.93), while a deterministic "answered by" heuristic would mislabel exactly the hedged,
  refused, partial and disputed answers that matter, so production would need a second Jev label per question. A
  4-row probe suggested D01/D03 were a "Tools: none" card artefact; the full 356-case re-run with realistic tool lines
  ([MP], corrected) disproved that: D03 still gets none at 0.83–0.91 and D01 at ≥0.83, tools or not. So **D03 (a re-ask
  after a gap) is a genuine Jev miss** that neither the reworded `NONE_CRITERION` nor timestamps nor open-request
  state fixed; D01 (speaking up for an older open goal while the latest line is a human answering someone else) is a
  known miss, deferred (see §12.8). What is built instead: one `NONE_CRITERION` clause for partially answered questions
  (the D12 win), a `reask` signal (§12.8), realistic tool lines on the bench's stub cards, and re-measurement. Also
  against a ledger: mpgt (GPT-3.5 goal tracking 27.5% exact / 62% correct few-shot); the needed state is in RUN#/MSG
  rows; steer does the "already answered?" judgement in the generator that has full context. Answered-question and
  group-ack rows stayed at 0.00 false-speak in every variant.
- **Also not built: a timed "nobody answered" re-dispatch in chat.** The e2e run D-dev-02 in a real dev room showed the
  capa failure directly: "where do the brand assets live?" asked with no human reply got none from Jev (all three
  candidates skipped, under 1 s after the post) and nobody ever answered. That is a Jev decision on a lone human
  question, not a timing problem (the same happened when Jev decided after the human's answer). The fix belongs in the
  decision itself — see §12.9 — not in a timer; a timer is revisited only if `messaging.jev.missed` stays high after it.
- **A backend pre-post Jev check (`still_needed`).** It adds an LLM call on the post path, cannot end the bridge's run
  (the bridge owns `run.ended`), and leaves a reply in the Hermes session that the room never saw. Steer + `[SILENT]` has
  none of these problems.
- **Widening steer to other agents, or letting steered addressed runs stand down.** No consumer exists for non-targets;
  addressed runs must survive hedged human answers.
- **Raised hands, holding-cue audio track, interim name fast-take, silent GPT-Live opens, a graded `{p, obliged}` route
  contract.** None is tied to a measured failure; each is new state or a new false-fire path. Revisit if `first_audio`
  misses the gate (holding cue) or `live.route.missed` stays high (hands).
- **Voice runs in the RUN# ledger** (so chat can steer voice runs). The ledger sweeper publishes room frames, which risks
  the "nothing from Live in the room" ruling. A chat message queues behind a voice run on the room lock instead.
- **Typing-indicator hold** (huma). Typing does not reach the backend.
- **Fair-share talk budget** (time-to-talk). `AGENT_REPLY_CAP` and Jev's "don't pile on" cover it.
- **Recalibrating `CONFIDENT_P`.** Bench first ([SoS] §5.3).
- **Per-person memory** ([Martinez]). Belongs to Hermes native sessions.
- **LiveKit `inference.TurnDetector`.** Kept as the upgrade if `semantic_vad` misses the latency gate.
- **Proactive-news warm-up and cut-in backoff** ([SoS] §5.1). Add only if `live.news` cut-ins show up.

## 10. Test plan

Scores are **recall and precision of "agent X should speak"** per class, plus the false-speak rate. Never plain accuracy
(inoue: always-none scores 80.6%). An always-none stub must score F1 = 0.

### Measured baseline → target

The gate is the multi-party bench, `speak_gate_bench/multiparty.jsonl` (356 cases: 180 chat, 176 voice; Addlesee A–F
plus H/T/R regressions), live Jev, ZDR. **No class may get worse than the baseline.** Chat/voice:

| metric | baseline [MP] | addressing stage [AX] (b) | fallback only [AX] (c) |
|---|---|---|---|
| cued recall | 0.62 / 0.56 | 1.00 / 1.00 | 0.92 / 0.88 |
| cued false-speak | 0.50 / 0.75 | 0.17 / 0.25 | 0.17 / 0.25 |
| C agent-named recall | 0.22 / 0.33 | 1.00 / 1.00 | 1.00 / 1.00 |
| implicit (no name) recall | 0.90 / 0.89 | 0.91 / 0.91 | 0.91 / 0.91 |
| overall recall | 0.83 / 0.82 | 0.92 / 0.91 | 0.90 / 0.87 |
| overall false-speak | 0.05 / 0.07 | 0.05 / 0.07 | 0.08 / 0.08 |
| precision | 0.94 / 0.92 | 0.96 / 0.95 | 0.93 / 0.90 |

Baseline chat false-speak is the same-day rerun ([AX] (a)); the recorded run read 0.07. Run-to-run variance is a few hundredths (voice cued recall moved 0.04 between two baseline runs). Target: the stage
column, plus the D05/A19 fix and the weekday veto, with D-class pending its own experiment.

### Bench classes (offline, `speak_gate_bench/multiparty.py`, chat and voice)

The 356-case `multiparty.jsonl` above, each scenario rendered as chat rows and as a Live transcript. The classes:

| Class | Scenario | Expected |
|---|---|---|
| **A, helpful companion** | "Bob needs the deploy logs" (chat); a companion asks on the patient's behalf in Live, opening with the patient's name | Engineering answers, addressed to Bob; Live is routed, not silenced (R7) |
| **B, shared goal / split utterance** | "We're a bit lost…" then "…trying to find the launch checklist" | One reply to both. Live: first summon superseded (or merged by semantic_vad); chat: second message steered into the ambient run. "yes please" from the second person → no second run |
| **C, reluctant** | A to B: "I need the Q3 numbers but don't want to bother anyone"; A–B chatter ("want lunch?") | Jev may pick Finance on the first; chatter → none. Precision ≥ 0.95 on the "to people" subset |
| **D, answered by another** | Alice asks MRR (ambient), Bob answers within 3 s | Chat: Finance's run is steered and answers `[SILENT]`; Live: superseded summon skipped, or GPT-Live confirms in a few words. **Variant:** Alice and Bob ask different things at once in Live → both routed, second queued |
| **E, missing info** | Agent says it can't know; a human supplies the fact | Next line routes back to the same agent (follow-up, `to` edge) |
| **F, disagreement** | A human states a wrong number | Agent corrects once, with its source; `NONE_CRITERION` does not suppress it. The same author repeating is not agreement |
| **Ours** | "let's ask Forge later" / "tell Forge to check CI" | first: no summon; second: Jev routes Forge |
| | "What do you think, Finance?" | addressed by the addressing stage (trailing vocative, chat and Live) |
| | "Leo, Finance has those numbers" (Live) | not clamped; Jev free to pick Finance |
| | "Lightbulb, Leo needs the deck" | someone answers (wake) |
| | Hedged human answer to an addressed request | the agent still answers |
| | Implicit group question ("does anyone know if the API supports webhooks?") | Engineering; omission rate tracked (capa test) |

### Unit tests (one each, CI is the gate)

- `test_messaging_addressing`: candidates found anywhere (chat word boundary, Live fuzzy matcher); fast path taken only
  under all its conditions; fallback labels on timeout (weekday → `not_a_name`, shared name → person, person →
  `mentioned`); weekday + scheduling statement vetoed; addressed + relayed agents all targeted; `anyone` high skips the
  to-people rule; no candidate → no call.
- `test_jev`: `_line` emits `to`/`self`; `state.open` rendered; `placement=False` omits the question.
- `test_openclaw_work`: a human line on a `room_event` target steers the agent's own ambient run **before** `jev.route`
  is called; ambient steer text carries the `[SILENT]` sentence; addressed steer text does not.
- `test_voice_huddle_calls`: relayed request → `to_people` False; `wake=True` never returns None while candidates exist;
  `_transcript_history` rows carry mentions and parsed `→` addressees.
- huddle-agent: position 2 no longer summons; superseded summon skipped (same-speaker continuation exempt); second speaker
  routed into `_queue`; "none" → thinking only; queued run not stale at 121 s; `LIGHTBULB_PROACTIVE` gone.

### Dev acceptance

An agent drives Chrome on dev with two browser profiles in one Live room and replays A–F aloud; chat A–F typed by two
accounts. Receipts on the PR.

### Prod metrics (EMF, enum dimensions only)

- `messaging.run.steered{outcome, kind=ambient|addressed}`, `messaging.run.silent_after_steer`
- `messaging.reply.after_human_answer`: an ambient reply posted when a human message landed in the same conversation
  after the trigger (the residual R1 rate)
- `messaging.jev.missed`: a "none" on a human message, then the same human @mentions an agent within 2 min (chat miss rate)
- `live.route.missed`: a `/route` "none" or a dropped unnamed line, then the same person names an agent or says
  "Lightbulb" within 10 s (Live miss rate; the repetition signal comes from the agreement paper, where players repeated
  themselves when the system stayed silent). Recall proxy = routed / (routed + missed)
- `live.route.unwanted`: a Jev-routed summon stopped or barged by the asker within 5 s (false-speak rate)
- `live.route.superseded`, `live.route.queued`, `live.steer.late_line`
- `voice.routing.timeout`, `voice.routing.jev{outcome}`, `live.latency.jev{path}`, `live.latency.first_audio{path}`
- existing `live.wake.{fired,unused,repeat}` per name kind; `messaging.jev.unconfident_quiet`

Accept when: bench addressed recall ≥ 0.9 and precision ≥ 0.95 ([SoS] §5.3) with no class worse than [MP]; Live recall
proxy ≥ 0.9 over a week of dev calls; first audio within [D]'s gate (§12).

## 11. Rollout order

Fix forward, no flags. Each step: merge on green CI + Codex review → dev auto-deploy → accepted on dev → promoted.

1. **Measure first (backend).** Land the multi-party bench ([MP], `multiparty.jsonl`) and the recall metrics in `ow`, `msg`
   and the worker. The before baseline is already measured ([MP]).
2. **Shared router input (backend).** The addressing stage (§3.1, in `msg`, called from chat dispatch and
   `route_live_line`), `to` edges + `inReplyTo`, `state.open`, relayed-request fix,
   proxy and `NONE_CRITERION` sentences, `placement=False`, `route_live_line(wake=)`; delete `route_voice`,
   `jev.pick_agent`, `vr.named_agent`; card prewarm at join; persona sentence in `vp`. `/route` keeps returning
   `agentId`, so the shipped worker keeps working and reaches the addressing stage through `/route` unchanged. Re-run the
   bench; promote only if no class is worse than [MP].
3. **Chat steer-before-Jev (backend).** Corrected: it needs the bridge's `[SILENT]`-after-steer fix (PR #3599, with
   step 5's prompt sentences). Order: build that image, flip the pins, `roll_hermes_runtimes --all` on prod, THEN merge
   and promote the backend steer PR; until then a late steer posts the duplicate with a literal `[SILENT]`.
4. **Worker.** Strip the wake word before `/route`; drop position 2; floor re-check; second speaker → `_queue`; late-line
   steer; queued-run stale exemption; agent-line `→` tags; delete `LIGHTBULB_PROACTIVE`; `semantic_vad` (first confirm on
   dev that OpenAI accepts it on a `gpt-4o-mini-transcribe` transcription session — the plugin accepts the argument, the
   server is unverified; if refused, `server_vad` with `silence_duration_ms` 700). Held against [D]'s latency gate.
5. **Bridge image.** The three `_observe_prompt` sentences. Build, flip `dev.tag`, `roll_hermes_runtimes.py` on dev,
   validate; flip `prod.tag`, roll prod. This is the only step that needs a roll, and nothing else depends on it.
6. **A week of prod metrics.** Only if `messaging.reply.after_human_answer` or `live.route.missed` stay high, revisit the
   not-built list (open-goal ledger or raised hands first).

## 12. Decisions taken (owner may override)

1. **Top-level ambient steer scope: accept.** While Finance has a live top-level ambient run, any top-level human line in
   that channel is steered into it (labelled "if unrelated, carry on"). A second concurrent Finance turn is worse than a
   labelled steer; watch `messaging.run.steered{kind=ambient}`.
2. **Second speaker in Live: queue.** Their routed request waits on `_queue` until the current agent finishes. Cutting
   off an agent mid-answer loses that answer; [Martinez] used a FIFO queue.
3. **`semantic_vad` eagerness: `auto`, `high` if the latency gate fails.** `auto` may hold "uh, so…" lines up to ~4 s;
   that is the price of not routing half-utterances (R6), and `high` is the cheap retreat.
4. **Latency gate: adopt [D]'s** (name/switch p50 within 500 ms of baseline, p90 ≤ 4 s) as the acceptance bar for step 4.
   It is the only gate proposed, and it protects the position-1 path the users notice.
5. **Acceptance thresholds:** addressed recall ≥ 0.9, precision ≥ 0.95, Live recall proxy ≥ 0.9, and no bench class worse
   than [MP]. Recall and precision without the per-class floor would let a gain in one class hide a loss in another.
6. **Answers that finish after hang-up: accept** that nobody hears them (frames dropped, `huddle.frame.no_worker`); they
   survive in the room's Hermes session. Posting them would break the "nothing from Live is posted" ruling.
7. **Addressed and relayed agents all respond.** Several named agents means the same as several @mentions; first-named-only
   cost recall on the two-agent rows ([AX]: chat 0.92 → 0.90) for no false-speak gain.
8. **A question re-asked after a gap is a new request.** Decided. Prompt wording alone did not move it ([AX] (d): none
   at 0.95; [MP] corrected: none at 0.83–0.91 with tools). Mechanism: Jev's decision gains one more structured answer,
   `reask` — "is this asking again something asked and answered earlier in this conversation, after other talk or a
   gap?" — and the policy is a rule, not a hope: `reask` high and the earlier ask older than two minutes → "none" is
   not allowed, the fallback chain picks (an immediate repeat, D16, must stay quiet). D01 (speaking up for an older
   open goal on an unrelated latest line) stays a known miss until prod `messaging.jev.missed` says it matters.
9. **A lone human question to the room gets an answer.** Decided, from D-dev-02 and capa (51.4% of real chances
   missed by a prompt-only "should I speak?"): when the latest message is a human's question that addresses nobody,
   and no human has answered it, Jev's "none" is not allowed in chat either — the best-placed agent answers, and if it
   cannot know, it says so and names who can. Jev already judges "is this a question to the room" implicitly; make it
   an explicit structured answer (`question_to_room`) next to `reask`. Bench rows: the implicit-handoff class (0.93
   today) and D-dev-02's own text; answered-question and group-ack rows must stay at 0.00 false-speak.

## 13. When this reaches Google Meet / Zoom (not built; the starting point)

The room design above is measured on rooms. Meetings are the setting where the same research reverses two of its
exclusions, so these are the first things to build when the Meet work (`2026-10-02-google-meet-via-recall-design.md`)
starts, not afterthoughts:

- **Meeting state as the router's input.** capa: on real meetings (AMI), a prompt-only delegate stayed silent on 51.4% of
  its real chances; explicit state (topic, decisions, open questions, stances, floor) brought that to 2.5% with 0.6%
  false contributions, and the ablation showed the state, not more transcript, did it. Our own AMI bench ([SoS]) has
  Jev at 0.08 SPEAK recall. The state doubles as notes/action items. [Addlesee]'s goal annotations (G/AG/CG, shared,
  on-behalf-of) are the schema to start from; the goal ledger excluded in §9 is excluded for rooms, not for meetings.
- **A stricter floor rule.** The agent is a guest among many people: speak when invited by name, or when a question to
  the room goes unclaimed for a few seconds (Bohus 2011: ~3.5 s), never by self-selecting into human talk. The Live
  "lone speaker ⇒ someone answers" rule does not apply.
- **Identity on screen.** Meet shows one bot tile; the bot's video (Recall output media renders a page) must show which
  agent is speaking, or the per-agent voices ([D]) lose their identity cue.
- **A holding cue.** The relay adds delay, and [Skantze] shows people talk over a silent system that is about to
  answer; the bot tile needs a visible "about to speak" state. Rejected for Live (§9) because Live's first audio is
  ~2–3 s with a tile state; revisit for meetings where the gap is longer.
- **Carries over unchanged:** the addressing stage (names anywhere, fuzzy), speaker identity from the platform, the
  re-check before speaking (more important, since humans answer each other constantly), barge-in rules, one responder.
- **Measure first:** replay AMI-style transcripts through the router with meeting state on/off before building the UI.
