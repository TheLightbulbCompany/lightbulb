# Voice v2 — Siri-style calls and room calls — design

Status: agreed in conversation 2026-10-01; awaiting written-spec review.
Builds on `2026-09-30-voice-calls-design.md` (v1: 1:1 calls, shipped as #3497/#3498/#3511).
Delete this file when Voice v2 ships.

## Goal

Talking to agents feels like Siri and is obvious to find: a floating orb that visibly
listens and speaks, and calls that work with one agent (DM) or with a channel's whole team
(room call) — the CEO's voice on the line, the right agent doing each piece of work live.

## Sequence (owner decision)

1. **Voice v2** — this spec: Siri-style UX + room calls, on the existing transport.
2. **Huddles** — several people + agents in one shared room, on LiveKit or Daily+Pipecat
   (research 2026-10-01: Daily+Pipecat ships a `gpt-live-1` service with client delegation;
   LiveKit is the close second). Push-to-talk addresses the agent there. Separate spec.
3. **Google Meet** — agents join a Meet via Recall.ai Output Media. Separate spec.

Voice v2 keeps routing, the roster, and per-agent turns server-side and transport-agnostic
so (2) only swaps "one person's WebRTC line to OpenAI" for a room.

## Owner decisions

| # | Decision |
|---|---|
| 1 | A call is with **one agent (DM)** or with **a room's team**; one voice either way. |
| 2 | Room calls: the **CEO's voice** speaks; **each request goes to the best agent live** (not CEO-delegates-via-Paperclip). |
| 3 | Routing: **a name you say wins** → else **Jev** → else **CEO**. |
| 4 | **Relay everything**: the voice hands every request to Hermes; it answers alone only for greetings, thanks, "say that again", "hold on". (A change from Hermes's own judgment-based policy, which in the 2026-10-01 dev test delegated nothing.) |
| 5 | Teammates not on the call see only a live **"📞 X is on a call with the team"** marker; nothing is posted after. Durable agent work shows up as usual. |
| 6 | **Floating orb** that stays while you use the app; **avatar ring** distinguishes DM vs room. |
| 7 | Entry point: a **labeled header button** only — "Talk" (DM), "Talk to the team" (channel). |
| 8 | **Open mic** (the model's native full duplex) + a **mute** button. Push-to-talk arrives with huddles. |
| 9 | Several people may each be on their own call at once (already true); free tier keeps one open call per workspace. |

## How the voice and the agent split work

Two brains: OpenAI's `gpt-live-1` is the ears and mouth — no tools, no workspace access; its
only power is firing a delegation. Hermes does every piece of work. Hermes only sees what the
voice delegates, so the voice instructions (ported from Hermes's desktop GPT-Live) are where
"relay everything" lives. Hermes's voice feature itself (desktop app + gateway) is not part of
what we run — we ported its persona, per-turn note and delegation loop.

## Section 1 — Experience (web; iPhone mirrors it in #3499)

**Start.** DM header button **"Talk"** → call with that agent, in its voice. Channel header
button **"Talk to the team"** → room call with the channel's agents, in the CEO's voice. The
bare phone icon goes away.

**The orb** floats bottom-right and survives navigation inside `/chat`.
- *Collapsed:* orb + one status line: *Listening…*, *Thinking…*, *Speaking*, *Finance is working*.
- *Expanded (click):* live captions (last two lines, both sides), who's working on what, time
  left, **mute**, **hang up**.

**States, driven by real audio** (Web Audio `AnalyserNode` on the mic track and on the remote
track):
- *Listening* — ripples with the caller's mic level.
- *Speaking* — ripples with the agent's output level.
- *Thinking* — slow pulse while a delegation is in flight with no tool activity yet.
- *Working* — progress ring on the working agent's avatar.

**Avatar ring.** DM: the agent's avatar sits in the orb. Room: the room's agents ring the orb
as small avatars; a working agent lights up and gets a chip below (*"Finance — checking
Stripe"*). Huddles later add people's avatars to the same ring.

**Channel marker.** While a room call is live the channel header shows *"📞 Prasiddha is on a
call with the team"* (several callers: names joined). Cleared on end, including a sweeper close.

**Errors** read in plain words inside the expanded orb: free minutes used, someone already on a
free call, voice not switched on, credits low, call dropped.

**Colours** come only from tokens (`--plane-raised`, `--fg`, `--fg-soft`, `--live`, …); motion
respects `prefers-reduced-motion` (static ring + status text).

## Section 2 — How it works

**1. Start.** `POST /api/v1/calls` takes `{agentId, agentName, sdp}` (DM) **or** `{roomId, sdp}`
(room). Room: caller must be a member; the team is the room's agent members whose listen mode is
not `observe`; at least one is required (else 409 `no_agents`). Billing, budget, holds, the free
claim, deadline and `close_call` are unchanged — still one OpenAI session per call. The call row
gains `kind: "dm" | "room"`, `roomId?`, `voiceAgentId`.

**2. Voices.** Each agent's voice is derived, not stored: the CEO (by `starter_agent_ids["ceo"]`)
is `marin`; every other agent gets a stable pick from GPT-Live's voice list by hashing its agent
id, so agents sound distinct and never change between calls. DM call → that agent's voice. Room
call → the CEO's voice if the CEO is a team member, else the first team agent's. The voice is
fixed for the session. (A per-agent voice setting is out of scope until someone asks.)

**3. Voice instructions.** One persona builder for both kinds:
- *Relay everything:* delegate every user request; answer directly only for greetings,
  thanks, repeat requests and "hold on"; never answer facts from its own knowledge.
- Room calls add the roster — each agent's name, role and one-line capability (from Jev's
  agent cards) — and the rule to attribute results ("Finance says…").
- While waiting: say briefly that someone is on it, then wait; speak quiet "working" notes only
  when asked.

**4. Routing (server-side).** `POST /calls/{id}/turns` stays `{utterance, transcript, seq}`; the
server picks the agent:
1. **Named agent** — the utterance starts with, or contains "ask / tell / get" + an agent's
   display name (case-insensitive, whole-word) → that agent, if on the team.
2. Else **Jev** — the existing decision call, framed as a message in the room with the call's
   recent transcript, asked to pick one team agent.
3. Else / Jev unsure / Jev down → **the CEO** (or the voice agent when no CEO).
DM calls skip routing. Response: `{turnId, agentId}`.

**5. One turn slot per agent.** The call row's single `turnRunId`/`turnSeq` become a map keyed
by agent: `turns.<agentId> = {runId, seq}`. Claim, supersede and verify (the numbered-request
protocol from v1) apply per agent: a new request to the same agent supersedes only that agent's
turn; different agents run in parallel. `close_call` stops every running turn. Each turn keeps its
own per-call Hermes session (`agent:<id>:lightbulb:call:<callId>`). Frames already carry
`agentId`; the client maps `delegation_id → {turnId, agentId}` and attributes thinking/commentary.

**6. Channel marker.** New room live event `room.call.changed {callId, userId, state:
"started"|"ended"}`, published to the room channel on start and from `close_call` (so crashes
clear). Clients keep the set of live callers per room and render the marker.

**7. Relay reliability check.** A scripted dev acceptance: 6 utterances that must reach an
agent ("what's the status of JEV-6", "check our Stripe payouts", …) and 4 that must not ("hi",
"thanks", "say that again", "hold on"). Pass = 10/10, counted from `/turns` requests in the logs.

**8. Verify first.** Whether `gpt-live-1` accepts a second delegation while one is in flight. If
not, the client queues it and the voice says *"one sec, Finance is still on the first thing"*.

## Out of scope (v2)

Huddles, Google Meet, push-to-talk, wake words, composer mic / global shortcut / first-run hint,
saved transcripts, per-call model choice, a per-agent voice setting.

## Testing

- Backend unit: room start (membership, team, no_agents, voice choice); routing (named agent,
  Jev pick, Jev unsure/down → CEO, name not on team); per-agent claim/supersede/verify and
  `close_call` stopping all turns; `room.call.changed` on start/close.
- Shared unit: delegation→agent mapping; parallel turns' frames attributed correctly; persona
  builder (relay rule, roster).
- Web: orb states from analyser levels (fake analyser), DM vs room ring, marker, mute, errors,
  reduced motion; header button labels.
- Dev acceptance: the relay script (§7), a room call where two agents work in parallel, the
  marker seen from a second account, hang-up billing.
