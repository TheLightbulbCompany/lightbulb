# Jev: who speaks in a room, proactive Listening, follow-up after interruption

Status: DRAFT for owner review (2026-09-22). No code until approved. Delete this file in the PR that ships the last slice.

## 1. What Jev is (researched)

TypeSafe AI's "System One" decision model. You send it state plus typed questions and get back calibrated probabilities. It generates no text.

- Question types: `noul` (yes/no probability), `choice` (up to 255 named options → chosen key + per-option probabilities + confidence), `score` (2–10 ordered levels).
- Endpoints: **OpenRouter Decisions API** (`POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13` or `~typesafe/jev-latest`, OpenRouter bearer key, `provider.zdr` supported, response carries `usage.cost`). The same model is on the **Vercel AI Gateway** as `typesafe-ai/jev`, through AI SDK `experimental_evaluate`. It is not reachable through an OpenAI-compatible client.
- Limits: 32k-token state. Price is $0.042 per 1M input tokens; output is free.
- Measured by third parties: p50 ≈ 450 ms, p90 ≈ 1.1 s, ≈ $0.000016 per call (souvikr/jev-test, 17 cases, one run).
- The docs disagree on the path (`/api/alpha/decisions` vs `/api/v1/api/alpha/decisions`). The slice-1 spike settles it with one curl.

**Decided (owner, 2026-09-22): Vercel AI Gateway, via its plain HTTP API.**
- Call: `POST https://ai-gateway.vercel.sh/v1/evaluate` with `Authorization: Bearer <key>`.
- Body: `{model: "typesafe-ai/jev", state, questions, providerOptions: {gateway: {zeroDataRetention: true, only: ["typesafe-ai"]}}}`.
- Question types are `boolean` / `choice` / `score`.
- The response carries `answers` and `providerMetadata.gateway.cost`. Per-request ZDR is Pro/Enterprise only, and the model is listed `zdr: all`.
- Price: $0.042 per 1M input tokens, output free (model list API). It is covered by the team's $5/month free gateway credit until the team's first credit purchase. The owner says their key is free.
- Key: Secrets Manager `lightbulb/{env}/vercel-ai-gateway` (owner-created, separate keys for dev and prod), read by the backend through `core/secrets_manager`.
- OpenRouter's Decisions API is the documented alternative and is not used.

## 2. What happens today (verified on origin/main eb60c16d8)

- `messaging.select_dispatch_targets` (called on the send path, before the `WORK#` rows are written in the create transaction):
  - `observe` never runs.
  - An @mention or any DM → `user_request`.
  - Plain traffic → `room_event` for every `active` (Listening, paid) agent.
  - The message's author is always skipped.
- `openclaw_work._dispatch_message` drops a `room_event` for a non-paid owner. Otherwise it **invokes the runtime for every active agent** (a cold start is 54–57 s).
- Inside the microVM, the bridge's `gate_ambient` (bridge/turn.py:266) makes **one Bedrock call per agent**: "DISPATCH or IGNORE". So today "proactive" means waking the microVM N times per plain message so that N independent gates can each say yes. Two agents can both say yes, and nothing chooses between them.
- Bug found: `gate_ambient` reads `participant.role` to judge relevance. `build_ingress_body` never sends a job role, so the gate always judges with an empty role.
- Follow-up (from the reliability track, 1f0222): nothing ever re-wakes an agent.
  - A run that errored, was aborted or was sweeper-closed is final.
  - A queued turn answers from history frozen when it was enqueued.
  - An agent→agent reply without an @mention is silent.
  - "Would Forge come back after Friday answered?" Today, no.

## 3. Design: one decision seam, in the work consumer

**Where it lives:** `openclaw_work._dispatch_message`, at the existing ambient/plan gate, before `create_run` and before the invoke. This means:

- **A "no" never wakes the microVM** (the 2026-09-16 ruling: the Listening gate belongs in the backend).
- **The send path stays as fast as it is.** Deliberate deviation from "inside `select_dispatch_targets`": that function runs inside the human send request, so a p90 of 1.1 s there would land on every send. The consumer already sits in front of a 0–57 s invoke, so a ~0.5 s decision there costs nothing the user can see.
- **No second event bus.** It is the same FIFO and the same `WORK#` rows. A follow-up adds one pointer kind on that bus.

`select_dispatch_targets` is unchanged. It still marks every active agent as a `room_event` **candidate**. Jev chooses among the candidates.

### 3a. Routing and proactive Listening (one question)

The question is asked only for `room_event` targets: ambient traffic, active members, paid plan. Mentions, DMs and observe stay deterministic (recommended option, "augment").

- One Jev call per message: a `choice` question, "who should act on the latest message?"
  - Options: each candidate active agent (key = agentId, criterion = name + job role), plus `none` ("chatter, acknowledgement, addressed to a human, or would prolong agent-to-agent back-and-forth").
  - State: the room kind and name, the last 10 messages (author kind/name, text ≤ 1,000 chars), the new message, and which candidates are mid-run.
- The decision is stored once per message: a `DECISION#<messageId>` row on the messaging table, with a TTL of 7 days.
  - Each candidate's consumer (one FIFO group per room+agent) computes the decision and does a put-if-absent. The loser of the race reads the stored row. The rare duplicate call costs about $0.00002, so no claim/lock is needed.
  - The SK is not `WORK#`, so the Pipe never sees the row.
- Wake the chosen agent if `probabilities[chosen] ≥ JEV_MIN_P` (start at 0.5). Every other candidate's run ends `ignored` without an invoke (metric `messaging.ambient_gate{outcome=jev_ignore}`).
- **Fallback:** on Jev error, timeout (3 s) or no key, behave exactly as today (dispatch to every candidate and let `gate_ambient` decide), and emit `messaging.jev.decision{outcome=fallback}`. An empty key disables the whole feature (the same "empty disables" pattern as `AGENT_EMAIL_DOMAIN`).
- Once enforcement is live, the bridge's `gate_ambient` is deleted in a later slice. The backend decision replaces it, and keeping both would mean two gates.

### 3b. Follow-up after interruption

Hook: `messaging._after_run_ended`, which already runs once per closed run, plus the sweeper's close in `run_ledger`.

When agent A's run ends:

1. Load the messages with seq > A's trigger seq that do **not** already target A. If there are none, stop.
2. Ask Jev: a `noul` question, "Given A's request, A's final reply (or that A's run errored or was stopped) and these newer messages, does A still owe a response or need to finish?"
3. If the answer is yes (≥ 0.6), write one `WORK#FOLLOWUP#<agentId>#<latestMessageId>` row. The new pointer kind `room_follow_up {roomId, agentId, messageId}` goes through the same Pipe to the same FIFO group (room+agent). The consumer calls the existing `_dispatch_message` with that newest message as the trigger and **the event kind of A's original run** (so it is a `room_event` only if it was one). The run id is `(agent, messageId)`, which makes it idempotent. **No bridge change:** it uses the existing event kinds and a fresh history read at dispatch, which also fixes "answers from stale history".
4. Bounds:
   - A follow-up run never spawns another follow-up unless a human message arrived after it.
   - At most 3 follow-ups per room+agent per hour.
   - The existing bridge loop guard (409) still applies.

Deliberately out of scope: "stopped short / promised but did not deliver". Per the 2026-09-16 ruling, a promise made in chat becomes a Paperclip task (skill rule, #2708), and the task's lifecycle is the wake. Revisit only if that rule is observed failing.

## 4. Latency, cost, billing

- **Latency:** +0.45 s p50 / ~1.1 s p90 on the consumer path, and only for ambient traffic in rooms with active agents. It removes one in-VM Bedrock call per candidate (the current `gate_ambient`) and removes every unneeded microVM wake. Hard timeout 3 s → fallback.
- **Cost:** about $0.00002 per ambient message, plus about $0.00002 per ended run that has newer traffic. At 10k ambient messages a day that is ≈ $0.20/day fleet-wide. It saves far more in avoided wakes and gate calls.
- **Billing:** absorbed by the platform. The owner's Vercel AI Gateway key (`lightbulb/{env}/vercel-ai-gateway`), which never touches the bedrock-gate or Metronome. The cost is recorded in the `messaging.jev.cost` metric from `providerMetadata.gateway.cost`.
- **Plan gating:** routing and proactive Listening apply only where Listening already applies (paid). Whether follow-up is paid-only is an owner decision (D4).
- **Privacy:** send `provider.zdr: true` (room text is customer data). If Jev has no ZDR endpoint, stop and ask.

## 5. Rollout (shadow first)

1. **Shadow:** decide and store the decision plus a metric, but dispatch exactly as today. On dev, compare Jev's pick with what the agents actually did: the run ledger `ignored` vs `completed`, and whether a reply landed. This is the "Jev as judge" measurement.
2. **Enforce routing:** the backend decision gates `room_event`, then delete `gate_ambient` from the bridge (image build → dev pin → roll → prod pin → roll).
3. **Follow-up wake** (backend only).

## 5a. Where replies land: thread or main channel (owner sign-off needed before building)

**Today's rule** (bridge `reply_thread_root`, turn.py:876):
- In a channel or group DM, every agent reply goes in the **thread under the message that woke it**. A top-level message becomes a thread root; a message inside a thread keeps that thread's root.
- A DM has no threads.
- Only humans (and the CEO's seeded welcome) post top-level.

**Proposal: keep that rule and extend it to the three new cases.**

| Case | Where it goes | Why |
|---|---|---|
| Routed answer (Jev picked Finance for a plain question) | Thread under the question | Same as a mention today. The channel shows one "1 reply · Finance" line, not a pile of messages. |
| Proactive (an agent speaks up unasked) | Thread under the message it reacts to | It is a reaction to something specific. The main channel stays human-paced. |
| Follow-up ("coming back to finish") | **The original request's thread**, not the latest message's | The asker is subscribed to that thread. An answer under someone else's message would be lost. |
| Agent-to-agent back-and-forth | Stays inside the thread it started in. It never creates a new top-level post. | This contains any ping-pong, and the `none` option plus the no-follow-up-from-agent-only-messages rule (D4) bound it. |
| An outcome worth everyone's eyes ("PR merged", "budget approved") | Thread reply, with Slack's "Also send to #channel" | This is the only way anything agent-authored reaches the main channel. It is opt-in per message, by the agent's skill rule, and only for outcomes. |

Mockup of #general after two plain messages, with Finance and Engineering Listening:

```
#general
────────────────────────────────────────────────
Aden   What did we spend on cloud hosting last month?
       └ 1 reply · Finance · 2m                         ← routed answer, threaded
Aden   lol the coffee machine is broken again
                                                        ← Jev: none. Nobody wakes.
Leo    @Engineering can you look at the flaky login test?
       └ 3 replies · Engineering, Finance · 5m          ← Engineering (mention) + Finance
                                                           spoke up in the thread (proactive)
Finance  ↪ replied in a thread: "Cloud spend is $412, 8% over budget"  [also sent to #general]
────────────────────────────────────────────────
Thread: "What did we spend on cloud hosting…"
  Finance   $412 across AWS + Vercel; details below…
  Engineering  (earlier interrupted) — back on this: the Vercel line includes preview builds…
                                                        ← follow-up lands HERE, in the original thread
```

**Implementation consequence** (why this needs sign-off): the bridge derives the thread from the *trigger* message. A follow-up triggered by the newest message would therefore land in the wrong thread when that message is top-level. There are two options:
- (a) Pick the newest message *inside the original thread* as the trigger. There is no bridge change, but if nothing new arrived in that thread, there is no trigger to pick.
- (b) Add one optional ingress field, `replyThreadRootId`, which the bridge prefers over the trigger's thread. This is a small bridge change, rolled in with slice 2.

Recommendation: **(b)**.

## 6. Decisions (2026-09-22, owner via coordinator)

- **D1:** Vercel AI Gateway, using the owner's key (§1).
- **D2:** Augment. Mentions, DMs and observe stay deterministic.
- **D3:** At most one ambient responder per message.
- **D4:** Follow-ups are paid-first, capped per room per hour, and never triggered by agent-only messages.
- **D5:** Shadow first.
- **D6:** Jev-as-judge stays in this track. The messaging session's test harness calls it.
- **New slice 0:** prove today's Listening end to end in dev before any Jev code (§7).
- **Open:** §5a thread placement, and option (a) vs (b) for follow-up threading.

## 7. Slice 0 findings (dev, 2026-09-22, org_3JhR0rr1ZGMFnZc2D6dQ8BHhh9G)

1. Upgrading from trial to paid leaves #general all `mentions`. The seed only runs on an empty room. Owned by Onboarding.
2. The plain-message path works up to the bridge: dispatchTargets = Finance `room_event`, and the run started 1.4 s after dispatch.
3. `gate_ambient` → `gate_error` in 1.9 s, with the bedrock-gate throttling "billing is not set up for this account". The user sees nothing. Visibility is owned by Onboarding.
4. A paid workspace can't infer until someone picks a provider AND adds credits. An @mention also fails (`model_unavailable`). Owned by billing/Onboarding.
5. Prod, last 7 days: 3 `room_event` runs in total (1 completed, 1 ignored, 1 gate_error). Listening is essentially unused.
6. Pending: rerun with working inference (a finance question, chatter, an engineering question) to record the gate verdicts, the replies and whether each reply is threaded.
