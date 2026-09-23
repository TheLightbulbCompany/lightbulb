# Messaging concurrency check (Chrome, before a messaging/bridge prod train)

Answers: do replies land in the right chat, and how much do concurrent turns slow
each other? Driven by an agent in Chrome (claude-in-chrome) against a PAID workspace.
Never the free tier: it shares one free-model budget with prod trial users. Use the
founder's prod workspace only with the owner's OK for that run.

## Setup
1. Sign in (Clerk sign-in ticket recipe in CLAUDE.md) and open `/chat`.
2. Create two channels, `check-a-<date>` and `check-b-<date>`, each with the same two
   agents on **Mentions**, so nothing else wakes.
3. In each check below, send `@<agent> Reply with exactly the token <T…> and nothing
   else. Do not use tools.` with a fresh token, and time from Send until the reply appears.

## Checks and expected results (baseline 2026-09-22, founder workspace)
| # | do | expect |
|---|---|---|
| 1 | one message to agent X in A (first of the session) | reply in ~7 s, plus any profile restore (see below) |
| 2 | again, same agent and room | ~5 s |
| 3 | agent X in A and in B, sent together | both correct; the second lands about one turn later (~+3 s). This is the per-agent lock (`bridge/turn.py` `_turn_lock`), by design |
| 4 | agent X in A and agent Y in B, sent together | both ~5 s, no queueing |
| 5 | two messages to X in A, back to back | answered in order |

Every reply must carry its own token, in its own room. A token in the wrong room is a
cross-talk bug: stop the train.

## If an agent is slow to start
In the runtime log (`/aws/bedrock-agentcore/runtimes/<runtime>-DEFAULT`), look for
`restored profile <agent>: N objects in Xs`. On 2026-09-22 Forge took 1023 s for 14,154
objects (sequential restore). #2908 parallelised the restore; re-measure after each
runtime roll and record the number on the PR that shipped it.

## Afterwards
Leave the two channels: there is no archive or delete route, and a room owner cannot leave
(#3009). Give them a suffix naming your run so the next person knows they are spent, and
remove the agent members if anything might still post in them.
