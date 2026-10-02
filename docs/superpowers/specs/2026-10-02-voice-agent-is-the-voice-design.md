# Voice: the agent is the voice (Option D) — design

Status: draft for owner review (2026-10-02). Replaces the "Lightbulb speaks for the team" model
from `2026-10-01-huddles-design.md` and `2026-10-01-voice-v2-design.md`. Delete when shipped.
Evidence: `.scratch-voice-identity-research.md`, `.scratch-option-d-verification.md` (repo root,
local), cited by section below as [R] and [V].

## Problem (root cause, owner-confirmed 2026-10-02)

Today the voice (OpenAI `gpt-live-1`) and the agents (Hermes) are two minds joined by a text pipe:
the voice is a memory-less, tool-less "Lightbulb" persona that relays everything; the agent answers
in first person; the voice paraphrases it ("…and me, Forge"). Every summon mints a new call id, so
both the voice session and the agent's Hermes session (`agent:{id}:lightbulb:call:{callId}`) start
fresh each time [R §1, §9]. It feels like two different entities.

## Decision

**The agent is the voice.** Whoever answers speaks as itself — its name, its personality, its own
voice — and its voice session hands work to *that same agent's* continuing Hermes session. There is
no separate "Lightbulb" character. This is how Hermes's own voice mode works (one entity; voice
turns go into the same chat session) [R §2–3], extended to many agents by giving each its own
GPT-Live session, opened only while it holds the floor. We keep OpenAI's speech-to-speech model.

"Lightbulb" stays only as the wake word meaning "ask the team"; whoever is routed answers in their
own voice. An agent's own name is also a wake word ("Forge, …").

## Experience

- **Every room** (channel, DM, group DM) has Live (already shipped). People talk to each other.
- **Summon:** say "Lightbulb, …" or an agent's name, or tap an agent's tile. The routed agent's tile
  lights, its voice answers. Only one agent speaks at a time.
- **Switching:** "Gauge, what do you think?" — Forge's voice yields, Gauge's opens and has heard
  everything said so far. While a voice is opening, its tile shows "joining…".
- **Several agents:** "Forge and Gauge, …" → they answer in turn. Work runs in parallel in the
  background regardless of who holds the floor; a finished result is announced by *its* agent in its
  own voice at the next quiet moment (proactive rules, #3559, ported to per-agent).
- **First voice conversation with a default-named agent** (CEO, Engineering, …): it opens with one
  line inviting a name ("I don't have a name yet — what would you like to call me?"). Once per agent,
  only while the default name stands.
- **"Remember…", "call yourself…", "be more blunt"** by voice go to the agent, which saves them; they
  apply to the rest of this conversation and to every future voice and chat session.
- **Nothing is posted to the room.** The voice conversation lives in each agent's own session and
  memory, not in the room timeline (owner, 2026-10-02).

## Architecture

### 1. Voices
- Each agent gets a stable voice from OpenAI's documented GPT-Live list (22 built-ins: alloy, ash,
  ballad, beacon, bossa, cedar, cinder, coral, delta, echo, gleam, marin, meridian, quartz, ripple,
  sage, shimmer, stone, tempo, verse, vesper, willow) [V §1]. The six starter roles get six distinct
  hand-picked voices (CEO = marin); others hash over the remaining list. Never `aster` (not in OpenAI's
  docs) [V §1]. Stored per agent (backend), so a later "change your voice" is a setting.
- Fallback: a rejected voice → `marin` (exists today).

### 2. Persona per agent (built at session start; immutable after) [V §6]
`build_agent_persona(agent)`: name, role, a ≤1,500-char excerpt of its SOUL.md, its **voice notes**
(§6), the room facts (room name; people and agents present), and rules:
- you are {name}; speak as yourself; delegate every work request to your backend self (same rules as
  today's relay policy, but "your backend" not "the team");
- answer on your own only small talk, "say again", "hold on", and questions about this call (who's
  here, which room);
- remember / rename / behaviour requests always go to your backend self;
- other agents' spoken lines in your history are theirs, not yours.
Instructions limit 16,384 tokens; this is ~1.5k [V §6].

### 3. Continuity: one Hermes session per agent per Live session
- A **Live session** = from first join until the room empties (worker job lifetime).
- Voice turns use session key `agent:{agentId}:lightbulb:live:{roomId}:{liveSessionId}` — every summon
  of Forge in this Live session continues the same Hermes conversation (bridge `call_session_key`,
  backend `_identity`).
- Billing stays per summon exactly as today (a call row per exchange: holds, free minutes, credits,
  usage); only the *session key* stops being per call.
- The Hermes turn input keeps today's context (room recent messages, speaker) plus the Live transcript
  delta since that agent last spoke.

### 4. Seeding each new voice session
- On open, the agent's GPT-Live session is seeded with the **shared Live transcript** (our per-person
  STT + every agent's spoken lines), trimmed to ≤128 messages and ≤8,192 tokens [V §3]: people's and
  other agents' lines as `user` with a name prefix ("Aden: …", "Gauge (agent): …"); this agent's own
  previous spoken lines as `assistant`.
- Mechanism: `Agent(chat_ctx=…)` at construction — `AgentSession.start()` has no `chat_ctx`;
  `update_chat_ctx` after start degrades to one thinking append [V §3]. We trim ourselves (the plugin
  caps at 128 items but does not count tokens) [V §3].

### 5. Routing and the first turn
- Routing happens **before** opening a voice, on our STT final of the summoning line: named agent >
  Jev > CEO (today's `voice_routing`). Tap = the last agent you spoke to, else the CEO.
- The new session never heard the summoning line, so it won't create a delegation for it [V, "what
  changes"]. The worker therefore **starts the Hermes turn itself** at routing time (same internal
  `/turns` route) and feeds the frames into the new session as `append_thinking`/`append_commentary`
  (no delegation id); the model is told "{speaker} asked: …; your backend self is on it".
- Within an exchange, follow-ups use GPT-Live's own delegation as today (same session, same agent).

### 6. Persistent personality ("voice notes") and names
- **Voice notes**: a short backend-owned per-agent text (≤1,000 chars) the agent can update
  (behaviour preferences like "be blunt", "don't say 'great question'"). Stored with the agent's
  config, **included in its SOUL.md generation and in every voice persona**, so profile rewrites never
  drop it (today SOUL.md is backend-owned and regenerated — an agent's own edit can be lost).
- **Rename**: a sanctioned self-rename the agent may call **only when a person in the conversation
  asks**; updates the Paperclip agent name and our room display name everywhere; the next voice
  session and the wake word use the new name.
- Both via a new hook-token internal route + a small skill instruction (agents must not rename or
  re-persona themselves unprompted).
- **Memory**: "remember X" → the agent's normal memory tool; voice turns already reach AgentCore
  memory [R §9]. Mid-session, the worker also `append_instructions(...)` the change so it applies now
  (instructions are immutable; append is allowed) [V §1/§6].

### 7. Floor, switching and timing
- One agent voice open at a time. Switch = start the new session **concurrently** with closing the old
  (don't wait for the old close; `update_agent` is serial and can wait up to 5 s) [V §5].
- Pre-warm only where predictable: a DM with one agent may keep that agent's session ready while the
  user is Live (billed as live: $0.05/min idle included [V §4]) — off by default; measure first.
- Turn-taking inside an exchange: unchanged (GPT-Live duplex with the pinned speaker; 20 s idle end;
  cut-ins per the reworked #3543).
- **Identity on the wire:** all sessions publish as the worker's one participant [V §5]; the worker
  sets a `lightbulb.speaking_agent` attribute (+ per-agent `status`/`working`) so clients show which
  agent's tile is talking/joining/working.

### 8. Cost
- Unchanged per minute: one GPT-Live session open at a time, $0.05/min billed per second while open
  [V §4]. Switches add each session's startup seconds. The 15 s minimum is a WebRTC-creation rule; the
  LiveKit plugin uses WebSocket, so it likely does not apply (inferred) [V §4].

## Unknowns to measure before shipping
1. **Startup latency** (WebSocket connect + `session.start` with persona+history → `session.started`
   → first audio). OpenAI publishes none; one forum report says 2–3 s [V §2]. Measure ~20 opens in
   prod LiveKit; if >2 s, design the switch UX around it (joining state + short cue).
2. Whether named-prefix `user` messages make the model treat other agents' lines correctly (inferred).
3. `append_commentary` without a delegation id for the driven first turn — confirm behaviour.

## Out of scope
Two agents speaking simultaneously; custom voices (consent recordings, 20/org cap, sales gate) [V §1];
cascade TTS mode (Option C) — fallback only if GPT-Live startup proves unusable.

## What changes (code map)
- Backend: `voice_persona.py` (per-agent persona; voice list), `voice_calls.py` (session key per Live
  session; first-turn driving route reuse; voice notes in turn note), new internal routes (rename,
  voice notes), `hermes_profiles.py` (voice notes into SOUL.md), skill text.
- Bridge: `call_session_key` accepts the Live-session key shape.
- Worker: per-agent session lifecycle (routing before open, concurrent switch, seeding, driven first
  turn, speaking-agent attributes, append_instructions on tweaks), proactive (#3559) per agent.
- Web/iPhone: tiles per agent with speaking/joining/working from attributes; Lightbulb tile removed.
