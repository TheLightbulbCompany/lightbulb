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

**Evidence from prod (reliability track, 09-21, founder org, #report-bugs):**
- 18:10:14: a human asks Forge to root-cause and fix a bug.
- 18:22:05: Forge dispatches a Hermes **async delegation** (`deleg_3e55140f`, a pre-commit review).
- 18:22:13: Forge ends its turn ("review is running now before I open the PR"), run outcome `completed`.
- 18:26:22: the delegate finishes. **Nothing wakes Forge**, so the PR is never opened and the fix sits uncommitted in the saved profile.

This adds two triggers the newer-traffic check above does not catch:

5. **Async-delegation completion wakes the parent** (APPROVED 2026-09-22; deterministic, no Jev call, original thread). When a delegate that a room run spawned finishes, the bridge reports it to the backend. That report is either a new hook-token event on the existing run-events route or a `run.ended` field `pendingDelegations`. The backend then writes the same `WORK#FOLLOWUP#…` row, with no Jev call, because it is deterministic: the agent itself asked for the result. The trigger is the original request, and placement is that run's own placement.
   - Bounded by the per-room+agent cap.
   - **Signal found (upstream hermes-agent):** `tools/async_delegation.py` pushes a completion event onto the shared `tools.process_registry.completion_queue`. The event carries `type: "async_delegation"`, `delegation_id` and the dispatching `session_key`, which for us is `agent:<id>:lightbulb:channel:<room>`. Upstream's gateway drains that queue (`gateway/run_notifications.py`) and injects an `[ASYNC DELEGATION BATCH COMPLETE …]` message into the session, which is what re-wakes the parent there. **Our bridge replaced that gateway and never drains the queue**, which is the Forge bug.
   - **Bridge hook:** a small drain loop in the bridge process. For each `async_delegation` event whose `session_key` is a room session, POST a `delegation.completed {sessionKey, delegationId}` hook-token event to the backend. The backend maps the session to the originating run and writes the `WORK#FOLLOWUP` row. The injected completion text becomes that turn's context, so the agent sees the result.
   - The delegate runs inside the same microVM, so the hook only works while that microVM is alive (4 h idle / 8 h max). A completion lost to a park is out of scope until observed.
   - It takes a bridge slot and is bundled with slice 2 (coordinator 2026-09-22).
6. **A turn that ends on a stated pending step** (HELD for the owner's ruling) ("X is running, then I'll do Y"). This is the "stopped short" case. The 09-16 ruling routes it to the Paperclip-task rule. Forge shows that the rule did not fire here: no task was filed. Proposal: after `run.ended` with outcome `completed`, one Jev `boolean` on the final reply, "Does this reply say the agent will do something after something else finishes?" A yes with **no** delegation pending means one follow-up wake after a delay (next sweeper tick ≥ 5 min), bounded by the same cap. Needs the owner's OK, because it reopens the 09-16 ruling.

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

## 5a. Where replies land: thread or main channel (owner decision 2026-09-22: "reply where the conversation lives")

Today's rule (bridge `reply_thread_root`, turn.py:876) is "always thread under the trigger" in channels and group DMs, and linear in DMs. The owner rejected "always thread": *some conversations live in the main channel, others in the threads under a message.*

**Rule:**

| Trigger | Placement | Decided by |
|---|---|---|
| The trigger message is **inside a thread** | That thread | Deterministic |
| A **top-level request or task** (an @mention with an ask, or a routed "can someone do X") | A thread under it. The work and any agent↔agent exchange stay there. | Jev `placement` answer = `thread`. An @mention on a top-level message defaults to `thread` if Jev is unavailable. |
| A **top-level message in an ongoing main-channel conversation** that an agent can add to (the proactive chime-in Jev exists for) | **Main channel, inline** (top-level) | Jev `placement` answer = `main` |
| **Follow-up** ("coming back to finish") | The original request's thread (or the main channel, if the original reply was inline) | Deterministic: the placement of the run it continues |
| **Outcome** of threaded work worth everyone's eyes | A thread reply plus "also send to #channel" | Agent skill rule; outcomes only |
| DM | Linear | Deterministic (unchanged) |

**Mechanism:** placement is a second question in the **same** Jev call (no extra latency or cost):
- `who`: choice among the candidates + `none`.
- `placement`: choice with `main` ("part of a flowing conversation in the channel; a short contribution") vs `thread` ("a specific request, task, or question that will take work or back-and-forth").

The backend passes the result to the bridge as an optional ingress field `replyThreadRootId` (a string = that thread; `null` = top-level). An old bridge ignores the field and threads as today, so this is N-1 safe. The field rides the slice-2 bridge change. Until then, everything threads, which is today's behaviour.

Mockup of #general with Finance, Engineering and Growth Listening:

```
#general
──────────────────────────────────────────────────────────────
Aden     we're thinking of moving the launch to next Thursday
Leo      works for me, marketing needs the extra days anyway
Growth   Thursday also lines up with the newsletter send — I'd    ← proactive, INLINE (main):
         hold the announcement for that, open rates are ~2x          a short contribution to a
                                                                     conversation living in #general
Aden     good call
                                                                  ← Jev: none. Nobody speaks.
Aden     can someone pull last month's cloud spend vs budget?
         └ 3 replies · Finance, Engineering · 4m                  ← a top-level TASK → Jev placement
                                                                     = thread; the work lives under it
Finance  ↪ replied in a thread: "Cloud spend $412, 8% over —      ← outcome, "also sent to #general"
           preview builds are the driver"  [also sent to #general]
──────────────────────────────────────────────────────────────
Thread: "can someone pull last month's cloud spend vs budget?"
  Finance      Pulling it from Stripe + AWS billing…
  Engineering  FYI preview deployments doubled after the 9/10 change
  Finance      $412 total, 8% over budget; the driver is preview builds.   ← Finance was mid-task when
                                                                              Engineering replied, and came
                                                                              BACK here (follow-up, same thread)
```

## 6. Decisions (2026-09-22, owner via coordinator)

- **D1:** Vercel AI Gateway, using the owner's key (§1).
- **D2:** Augment. Mentions, DMs and observe stay deterministic.
- **D3:** At most one ambient responder per message.
- **D4:** Follow-ups are paid-first, capped per room per hour, and never triggered by agent-only messages.
- **D5:** Shadow first.
- **D6:** Jev-as-judge stays in this track. The messaging session's test harness calls it.
- **New slice 0:** prove today's Listening end to end in dev before any Jev code (§7).
- **§5a:** approved with the amendment "reply where the conversation lives". Placement is a Jev output. Option (b), the `replyThreadRootId` ingress field, is approved; it is N-1 safe and rides the slice-2 bridge change.

## 7. Slice 0 findings (dev, 2026-09-22, org_3JhR0rr1ZGMFnZc2D6dQ8BHhh9G)

1. Upgrading from trial to paid leaves #general all `mentions`. The seed only runs on an empty room. Owned by Onboarding.
2. The plain-message path works up to the bridge: dispatchTargets = Finance `room_event`, and the run started 1.4 s after dispatch.
3. `gate_ambient` → `gate_error` in 1.9 s, with the bedrock-gate throttling "billing is not set up for this account". The user sees nothing. Visibility is owned by Onboarding.
4. A paid workspace can't infer until someone picks a provider AND adds credits. An @mention also fails (`model_unavailable`). Owned by billing/Onboarding.
5. Prod, last 7 days: 3 `room_event` runs in total (1 completed, 1 ignored, 1 gate_error). Listening is essentially unused.
6. Pending: rerun with working inference (a finance question, chatter, an engineering question) to record the gate verdicts, the replies and whether each reply is threaded.
