# Understanding bench (2026-10-04)

112 scenes, 3 runs each, live Jev on the dev gateway, stub cards. `understand.py --runs 3`.

## Baseline: the router before conversation understanding

`/route` returns agents only; each routed agent gets the whole line (or its phase-1 task when two or
more agents are named). The floor speaker's lines do not reach `/route` at all in production (the
voice model hears them), so the real baseline for those lines is one blob for the floor agent.

| Metric | Baseline | Gate |
|---|---|---|
| Request recall | 144/312 = 0.46 | 0.95 |
| Extra rows | 165/309 = 0.53 | <= 0.03 |
| Work started for nothing | 75/102 = 0.74 | <= 0.02 |
| Owner allowed | 132/144 = 0.92 | 0.90 |
| Scenes passing all runs | 53/112 = 0.47 | 0.85 |
| Latency p50 / p95 | 320 ms / 860 ms | |

Zero scenes pass in: announce, batch-domains, batch-one-agent, cancel, named-plus-unnamed, social,
social-plus-request, steer. Every greeting, thanks and "hmm okay" starts a run.

## Conversation understanding (`ops`)

One Jev call per line: the line is cut at names, sentence ends and where a new request begins (word
rules); per part Jev says what it is and whose job it is. Among two or more people with no name, chat's
room judge still says whether an agent answers at all.

### Blind sets: the honest number

Three sets were written by separate agents that saw neither the code nor the other sets. Each was run
once (3 runs) on committed code before anything was changed for it.

| Blind set, first run | Request recall | Extra rows | Work started for nothing | Scenes passing all 3 runs |
|---|---|---|---|---|
| Baseline router (tuned set) | 0.46 | 0.53 | 0.74 | 0.47 |
| `understand_holdout.jsonl` (94) | 0.84 | 0.16 | 0.07 | 0.83 |
| `understand_holdout2.jsonl` (100) | 0.92 | 0.06 | 0.01 | 0.92 |
| `understand_holdout3.jsonl` (100) | 0.90 | 0.06 | 0.00 | 0.93 |

So on speech it has not seen, the stage puts about nine asks in ten in a row of their own. The misses
are a long tail of phrasings, almost all in run-on speech with no punctuation. Each blind set showed
five to seven new classes; each class was then fixed in the word rules, never a single row.

A missed cut is not a dropped ask: the two asks run as one request (the turn note says "if it holds
more than one, do each"). The harness reports that as "asks that reach an agent".

### After the class fixes (all four sets are now development sets)

| Set | Asks that reach an agent | Request recall | Extra rows | Work started for nothing | Owner allowed | Pass all 3 runs |
|---|---|---|---|---|---|---|
| `understand.jsonl` (112) | 1.00 | 1.00 | 0.00 | 0.00 | 0.99 | 0.99 |
| `understand_holdout.jsonl` | 0.99 | 0.96 | 0.04 | 0.07 | 1.00 | 0.95 |
| `understand_holdout2.jsonl` | 1.00 | 1.00 | 0.00 | 0.00 | 1.00 | 0.99 |
| `understand_holdout3.jsonl` | 0.99 | 0.98 | 0.03 | 0.00 | 0.98 | 0.97 |

Latency of the stage: p50 340-480 ms, p95 660-1130 ms (baseline 320 / 860 ms).

What still fails: one verb with two objects for two owners ("I want the invoice sent plus a list of
open PRs"); a comma list whose verb takes no article ("reconcile last month's payouts"); two lines
among people where the room judge picks an agent (the judge is unchanged from before); an owner
that is arguable ("pull the signups" to marketing or analytics).

### Regression gates

| Bench | Before | After | Gate |
|---|---|---|---|
| Multiparty, voice, total recall | 0.93 | 0.93-0.95 | no class worse by > 0.03 |
| Multiparty, voice, precision | 0.93 | 0.95-0.96 | >= 0.95 |
| Multiparty, voice, named-agent recall | 0.92 | 1.00 | >= 0.90 |
| Phase-1 split bench | 0.98 | 0.97-0.99 | >= 0.95 |

One multiparty class reads worse: T (threads), one row (T03). The old bench's voice variant passed the
row's thread id to the judge; a voice call has no threads, and the bench now calls the real router.

### Two things that were tried and dropped

- **Part typing in place of the room judge.** Multiparty voice recall fell from 0.92 to 0.49 (people
  talking; an agent should chime in). The judge is kept for lines among people.
- **Jev judges every possible cut** ("does something new start here?", words only propose cuts).
  Recall 0.88-0.92 on the three sets against 0.92-0.99 with the word rules. Jev types a part well and
  judges a cut poorly ("send the invoice and check if Brightway paid" read as one thing).

## Side talk, fragments and complaints (2026-10-04, after two prod calls)

A lone caller's microphone also hears what they say to someone beside them. With a voice open (`mine`)
or one person in the call, every line is "implied" to be for the agents, and these became runs:
"Ubera business, right" (added to Pulse's working request as a correction), "Can you not talk on me",
"Sorry, I get nervous around, but um, do", "do".

20 `sidetalk` scenes (11 that must make no request, 9 real asks that are easy to confuse with them),
3 runs each:

| | Extra rows | Work started for nothing | Scenes passing all 3 runs |
|---|---|---|---|
| `main` before the change | 12/39 = 0.31 | 12/33 = 0.36 | 15/20 |
| after | 3/30 = 0.10 (one scene) | 3/33 = 0.09 (one scene) | 19/20 |

Whole file after the change (132 scenes): request recall 339/339, extra rows 3/342 = 0.01, work started
for nothing 3/135 = 0.02, scenes passing all runs 130/132. `understand_holdout3.jsonl` is unchanged
(0.99 / 0.98 / 0.03 / 0.00 / 0.98 / 0.97).

What moved the numbers:

- The kind criteria (`messaging.KINDS`): `people` also covers someone in the speaker's own room who is
  not on the call; `social` also covers a fragment or false start and a complaint about the call. This
  alone clears every prod line but one.
- `CONTEXT_P = 0.6`: a part read as background below this is not for the agents. "Ubera business,
  right" read `context` at 0.42-0.47 and was steered into a working request; the weakest true
  correction reads 0.75.
- `REQUEST_P = 0.5`: a request read weaker than that, or no read, starts no run. It changes nothing on
  these sets; 0.6 already loses the two-word follow-up "and last month?" (0.54-0.62).
- With the classifier down, a line that is implied only by `mine` or by a lone caller is no longer one
  whole-line request (a named agent, the wake word and an unclaimed question still are).

One scene was moved out of the file as a known limit: "These are the agents, and Aiden and Leo are in
there as well. How many agents do you have", said to a person beside the caller. Its second sentence is
a well-formed question to "you" and reads `request` at 0.98. Words alone cannot tell who it was said to;
that needs to know who is speaking to whom (voice identity, or gaze in a room).
