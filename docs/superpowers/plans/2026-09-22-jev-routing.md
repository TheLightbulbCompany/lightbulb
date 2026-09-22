# Plan: Jev routing / proactive Listening / follow-up

Spec: `docs/superpowers/specs/2026-09-22-jev-routing-proactive.md`. Blocked on owner approval (D1–D6). Worktree `.worktrees/jev-routing`, branch `feat/jev-routing`. Delete this file with the spec.

## Slice 1: shadow decision (backend only, one PR)

1. **Spike (no code).** Make 10 curls to `https://ai-gateway.vercel.sh/v1/evaluate` with the dev key: record latency p50/p90, the response shape and `zeroDataRetention` acceptance.
2. **Infra.** The owner creates the secret `lightbulb/{env}/vercel-ai-gateway`. CDK threads its name into the backend env as `VERCEL_AI_GATEWAY_SECRET_ID` and grants read. Run `npx cdk synth` and commit the regenerated `deploy.yml`.
3. **`core/services/jev.py`** (new, ~60 lines):
   - `async decide(state, questions) -> dict | None` using `httpx`, a 3 s timeout, `zdr: true` and one attempt.
   - Returns `None` on any failure.
   - Emits the metrics `messaging.jev.decision{outcome}`, `messaging.jev.latency` and `messaging.jev.cost`.
   - TDD with httpx mock transport.
4. **Agent job role.** Find the source of a real role per agent (the Paperclip agent `title`/`role`, via the existing roster read, cached per owner for 10 minutes). Fall back to the display name.
5. **`core/services/openclaw_work.py`.** At the `room_event` gate: build the choice question over all `room_event` candidates for that message (read from `message.dispatchTargets`), call `jev.decide`, and put-if-absent `DECISION#<messageId>` via `messaging_repo`. In shadow mode, **dispatch is unchanged**; only the row and a metric are written. Tests:
   - the decision is stored once across two candidates;
   - a race loser reads the stored row;
   - fallback on `None`;
   - mentions, DMs and free-plan messages never call Jev.
6. **`messaging_repo`.** Add `put_decision_if_absent` / `get_decision` with a TTL attribute. Tests.
7. **Dev acceptance.**
   - Set up a `+clerk_test` workspace labelled "jev" on a paid plan, with two agents Listening in #general.
   - Send 20 scripted ambient messages (chatter, finance question, engineering question, acknowledgement, agent-to-agent).
   - Read the `DECISION#` rows and run ledger outcomes, and paste the comparison table into the PR.

Files: `core/services/jev.py` (new), `core/services/openclaw_work.py`, `core/repositories/messaging_repo.py`, `core/config.py`, `apps/infra/lib/stacks/service-stack.ts` (+ the regenerated `deploy.yml`), and tests.

## Slice 2: enforce routing and delete `gate_ambient`

- Backend: when the decision says not-me, end the run as `ignored` with no invoke. Wake the chosen agent only if `p ≥ JEV_MIN_P`.
- Bridge: delete `gate_ambient` and its prompt and tests.
- Image build → dev pin → roll → dev acceptance (same 20 messages; exactly one reply per routed message, zero replies to chatter) → prod pin plus a roll, **with the owner's go**.

## Slice 3: follow-up wake

- `_after_run_ended` plus the sweeper close: the newer-traffic check, a Jev `noul`, and a `WORK#FOLLOWUP#…` row.
- The `room_follow_up` pointer kind in `deserialize_pointer` and a consumer branch that calls `_dispatch_message`.
- Bounds: no chaining without a human message in between; 3 per room+agent per hour.
- Check the Pipe filter (`api-stack.ts`) still matches `WORK#`.
- Dev acceptance: reproduce the Forge sequence (A mid-task, B answers, A comes back once).

## Overlaps

- `core/services/messaging.py` (`_after_run_ended`) and `openclaw_work.py`: the messaging session (c1ec3f).
- The paid-Listening gate: the OpenRouter billing session (aa91ce).
- The bridge (`turn.py`): the reliability session (1f0222).
- Coordinate through lightbulb-nosync-eb before each PR.
