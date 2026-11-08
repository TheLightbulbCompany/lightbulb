# GPT-Live contract on our stack (Task 0 spike)

8 Nov 2026. Measured with `apps/huddle-agent/spikes/gpt_live_spike.py` on `livekit-plugins-openai` / `livekit-agents` 1.8.11 (the `uv.lock` pins, synced with `uv sync`).

**How it was run:** each session talked to the plugin directly (`GPTLiveModel(delegation="client").session()`), with no LiveKit room. Speech was macOS `say` audio at 24 kHz, pushed in 100 ms frames at real-time pace. Each delegation got a fixed stub answer after 2.0 s, standing in for the Router. All timings are wall-clock times in the spike's process on the dev Mac. No dev orb session or dev LiveKit room was used, so the room hop (agent ↔ LiveKit SFU ↔ client) is **not** included.

Three sessions, all on the dev OpenAI key (`lightbulb/dev/openai_api_key`):

| run | input | billed |
|---|---|---|
| 1 | "What have we shipped this week?" | 23 s |
| 2 | "Hi, can you hear me?", then 4 s of silence, then the question | 27 s |
| 3 | the question, then "Wait, hold on, just the first one please." pushed 1 s after the stub answer started playing | 20 s |

Labels:
- **[M runN t=…]**: measured; the value is the run's JSONL log line at that time since start.
- **[D url]**: documented.
- **[C file:line]**: read in code.
- **[U]**: unverified.

## 1. Model and connection
- **Model id `gpt-live-1`.** `session.started.session.model == "gpt-live-1"` and the session id looks like `live_u0_…`. [M run1 t=1.163]
- **Connection from a LiveKit worker:** the plugin opens the WebSocket itself at `wss://api.openai.com/v1/live/sessions` with `Authorization: Bearer`. No WebRTC transport or sideband is needed. [C gpt_live.py:669-681, 1360-1370]. Connect to `session.started` took 0.36–0.56 s after `session.start` was sent. [M run1 0.762→1.163, run3 0.301→0.862]
- **In an `AgentSession`:** `AgentSession(llm=GPTLiveModel(...))` is accepted. A `DuplexModel` is wrapped in `DuplexRealtimeAdapter` [C livekit/agents/voice/agent_session.py:416, :655]. The plugin session (with `append_commentary` and the `delegation_created` event) is reached through `agent.duplex_session()` [C voice/agent.py:698]. This drops into the existing `llm=` slots: owner.py:136, meet.py:290, phone.py:593.
- **Session config the plugin sends** [M run1 t=0.762]:
  ```json
  {"type":"session.start","session":{"model":"gpt-live-1","instructions":"…","audio":{"format":{"type":"audio/pcm","rate":24000},"output":{"voice":"marin"}},"delegation":{"type":"client"}}}
  ```
  The session echoes `"delegation":{"type":"client"}`, `"status":"active"` and `"input":[]`.
- **Immutable after start:** instructions, voice and history. `_update_instructions` raises after start. [C gpt_live.py:1246-1252] [D https://docs.livekit.io/agents/models/realtime/plugins/gpt-live]
- **Agent tools under client delegation are refused, not ignored.** Any `@function_tool` on the agent raises `RealtimeError` ("has no tool channel"). The agent's tools list must be empty. [C gpt_live.py:1254-1265]
- **Audio:** PCM16 mono at 24 kHz both ways. The plugin resamples input [C gpt_live.py:1166-1179]. Output silence arrives as all-zero frames. [M: LOUD=50 RMS split speech from silence cleanly in all runs]

## 2. Client delegation events (exact payloads)
**Request.** It arrives right after the input-transcript delta of the caller's last word. [M run1 t=3.537, run2 t=9.192, run3 t=3.369]
```json
{"type":"session.delegation.created","offset_ms":2800,
 "delegation":{"id":"item_Ei3Zsdoc3w6AKjOB3S1Vu","type":"delegation","target":"client"},
 "event_id":"event_…"}
```
- `delegation.type` is the literal `"delegation"`, `target` is `"client"`, and the id starts with `item_`.
- The event does not carry the utterance. [M; D https://developers.openai.com/api/docs/guides/live-delegation]
- `offset_ms` is on the model's audio clock. It equalled the `start_ms` of the caller's last word in runs 1 and 3 (2800, 2600), and was 200 ms past that word's `end_ms` in run 2 (8800 vs 8600). [M]

**Plugin event `delegation_created` → `GPTLiveDelegation(id, pending_transcript)`.** [C gpt_live.py:950-961]

**Result** (what the app sends, plugin `append_commentary(text, delegation_id=…)`) [M run1 t=5.543]:
```json
{"type":"session.commentary.append","event_id":"append_9c3e02bba382","delegation_id":"item_Ei3Zsdoc3w6AKjOB3S1Vu","content":"This week the team shipped three things: …"}
```
**Receipt** [M run1 t=6.257]:
```json
{"type":"session.commentary.appended","start_ms":6000,"end_ms":6200,"client_event_id":"append_9c3e02bba382"}
```
The receipt marks when the content entered the model's context. It is not end of speech. [C gpt_live.py:844-857]

Also available: `session.thinking.append` (silent context) and `session.instructions.append` (a standing rule), each capped at 500 tokens [D live-delegation]. Neither was exercised.

**Other server events seen** [M]:
- `session.input_transcript.delta {delta, start_ms, end_ms}`, in 200 ms buckets
- `session.output_transcript.delta {delta, start_ms, end_ms}`
- `session.output_audio.delta` (base64 PCM)
- `session.usage.updated {usage:{seconds}, context_window:{usage_ratio}}`
- `session.closed {reason:"close_requested", usage:{seconds}}`

## 3. Does it delegate without question-type rules?
**Yes.** The prompt below is OpenAI's template from https://developers.openai.com/api/docs/guides/live-prompting, filled in with Lightbulb's name and the Router's capabilities only, using the literal `Generate <|SpawnThinking|>` wording for gpt-live-1.
- "What have we shipped this week?" delegated in 3/3 runs. [M run1 t=3.537, run2 t=9.192, run3 t=3.369]
- "Hi, can you hear me?" did **not** delegate. It answered itself, "Yeah, I can hear you fine.", starting before the caller's last word ("me") was transcribed. [M run2 t=3.277–4.061]

Exact prompt used:
```text
You are Lightbulb, a calm, friendly voice assistant for the Lightbulb team.
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
If the user is frustrated, acknowledge it briefly and focus on the next helpful step.

Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the user interrupts. Listen to what they say.

Delegation policy:
Backend tools:
- The Router: the team's brain. It knows the team's work across GitHub, email, calendar and docs.
- Memory: the Router remembers and recalls what the team has said and decided.
- Work threads: the Router starts work threads that hand tasks to the team's agents.

Generate <|SpawnThinking|> when:
- The request needs a backend capability or careful reasoning.
- A correction changes the work already requested.

Do not generate <|SpawnThinking|> when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Generate <|SpawnThinking|> before giving an answer that depends on backend work.
Do not guess the result while waiting.
```

## 4. `pending_transcript` is not reliable on its own
- Runs 2 and 3: complete, `" What have we shipped this week"`. [M run2 t=9.196, run3 t=3.372]
- Run 1: **only `" week"`**. [M run1 t=3.538]
- **Cause:** the plugin ends the caller's turn once 800 ms of input audio has been pushed with no new transcript fragment [C gpt_live.py:1159-1164, `_MIN_SILENCE_MS`]. In run 1, "this" arrived at t=2.653 and "week" at t=3.536, a gap of 0.88 s of transcript lag rather than of speech. The turn split there, and "What have we shipped this" went to the chat context as its own user message.
- **Contract:** build the ask from the last user messages in `chat_ctx` plus `pending_transcript`. Alternatively, collect `session.input_transcript.delta` with `start_ms <= offset_ms` since the previous delegation. Never use `pending_transcript` alone.

## 5. Timings (wall clock, this Mac → api.openai.com, no LiveKit room)
"User end" is when the last frame of the WAV was pushed. The `say` WAV may include a little trailing silence [U].

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| user end → `delegation.created` | 1.03 s | 1.19 s | 0.86 s |
| **user end → first audio** (the model's own ack) | **1.21 s** | **1.81 s** | **1.14 s** |
| `commentary.append` sent → `commentary.appended` | 0.72 s | 0.62 s | 0.57 s |
| **commentary sent → first result word transcribed** | **0.53 s** | **0.65 s** | **1.39 s** ¹ |
| commentary sent → first result audio | 0.68 s | 0.75 s | ~1.57 s ¹ |

¹ In run 3 the ack was still being spoken ("Let me check our updates for this week. One moment."). The model finished the ack, then said "Okay. This week, …".

- Input transcript deltas trail the caller's audio by about 0.4–0.9 s. [M: e.g. run3 "week" start_ms 2600, arrived t=3.368]
- Output audio streams at about real time: output `start_ms` tracks the wall clock. The ack's text arrives about 0.1–0.2 s before its audio. [M]
- **Automatic acknowledgement:**
  - It always played and was always the first audio: "Let me pull up this week's highlights." / "I'll check." / "Let me check our updates for this week. One moment." [M run1 t=3.568, run2 t=9.682, run3 t=3.542]
  - The plugin does not expose `delegation.acknowledgement_delay_ms`. There is no field in `gpt_live_types.py` [C grep]. Sending a raw `session.update` with that field is untested [U].

## 6. Verbatim or paraphrase: a light paraphrase that kept every fact
Stub sent: "This week the team shipped three things: Apps hosting on the Vercel free tier, the phone receptionist on dev, and CLI version 0.9.11 with the A2A issuer key."

| run | spoken | what changed |
|---|---|---|
| 1 | "Three things. Apps hosting on the Vercel free tier, the phone receptionist on dev, and CLI version 0.9.11 with the A2A issuer key." [M run1 t=6.071–15.957] | dropped the lead-in |
| 2 | "We shipped apps hosting on the Vercel free tier, the phone receptionist on dev, and CLI version 0.9.11 with the A2A issuer key." [M run2 t=11.847–20.629] | rewrote the opener and dropped "three things" |
| 3 | "Okay. This week, …" (interrupted) "Yep. We shipped apps hosting on the Vercel free tier." [M run3 t=6.767–13.49] | honoured "just the first one" |

- The opening framing is rewritten and the lead-in is dropped.
- Names and numbers came through exactly: Vercel, free tier, 0.9.11, A2A issuer key.
- Nothing was added in 2/2 full answers.
- OpenAI documents that commentary is paraphrased by design [D live-delegation]. n=2 is too few to claim it never adds or drops facts.

## 7. Interruption [M run3]
- The interrupt audio started at t=7.005, while the model was saying "This week,". The model's output transcript stopped after "week," (t=7.934), and "Wait" was transcribed at t=8.355.
- The app sent nothing: there is no client cancel or truncate event [D https://docs.livekit.io/agents/models/realtime/plugins/gpt-live]. The model stopped by itself.
- During the caller's speech, two short audio bursts (100–200 ms) came with no transcript (t=8.357, 9.354). They are probably backchannels [U].
- After the caller finished, it said "Yep." and gave only the first item. It used the correction from context and did not delegate again.
- Not measured: whether audio already buffered in a LiveKit room plays on after the model stops. That is the `DuplexRealtimeAdapter` playback path.

## 8. Price and cost
- **$0.05 per minute, billed per second.** Backend and tool use are billed separately. [D https://developers.openai.com/api/docs/models/gpt-live-1, https://developers.openai.com/api/docs/pricing]
- `usage.seconds` comes in whole seconds and tracks wall time from `session.started` to `session.closed`. [M]
  - Run 1: 21.7 s of wall time, billed 23 s.
  - Run 2: 26.6 s, billed 27 s.
  - Run 3: 20.3 s, billed 20 s.
- The meter runs while the caller and the model are both silent. Billing is for session duration, not speech. [M run1: 16 s at t=16.269, 23 s at close]
- **Spent in this spike: 70 billed seconds ≈ $0.058** across 3 sessions. The test audio came from macOS `say`, which is free.

## 9. Account access
- The **dev** OpenAI key (`lightbulb/dev/openai_api_key`) opens `gpt-live-1` sessions with no error. [M run1–3 `session.started`]
- Not checked: whether this is the same key, or the same org and project, as the `OPENAI_API_KEY` LiveKit agent secret. I didn't read the prod secret or the LiveKit secrets. [U]
- Concurrency limits by tier: Build 50, Launch 300, Grow 500; GPT-Live is not on the Free tier. [D models/gpt-live-1]. Our tier was not checked [U].

## 10. SIP and phone
- `GPTLiveModel` takes the same `AgentSession(llm=…)` slot that phone.py:593 uses for the LiveKit SIP leg, so the SIP transport is unchanged. [C agent_session.py:655]
- No call was placed. [U]
- OpenAI also offers native SIP into GPT-Live, at `sip:$PROJECT_ID@sip.api.openai.com;transport=tls`, as a fallback. [D https://developers.openai.com/api/docs/guides/voice-sip]
- Phone audio is 8 kHz and the plugin resamples to 24 kHz [C gpt_live.py:1168-1173]. Its effect on turn detection is untested [U].

## 11. What later tasks code against
1. `GPTLiveModel(delegation="client")` with no agent tools. The prompt is the template in §3.
2. On `delegation_created`, build the ask from `chat_ctx` plus `pending_transcript` (§4). Post it to the Router. When the Router replies, call `append_commentary(reply, delegation_id=d.id)`. Several appends with the same id continue the same delegation.
3. Expect the model's own ack about 1.1–1.8 s after the caller stops, and the Router's answer spoken about 0.5–0.8 s after `append_commentary` when the model is idle (about 1.4 s if the ack is still playing).
4. Results come out lightly paraphrased with facts kept. Exact wording would need our own audio.
5. Interruptions are the model's own job. The app sends nothing.
