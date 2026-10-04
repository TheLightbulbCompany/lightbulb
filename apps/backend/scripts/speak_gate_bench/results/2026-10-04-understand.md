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

One Jev call per line: the line is cut at names, sentence ends and request cues; per part Jev says what it
is and whose job it is. `--runs 3 --tag v2`, FIT_P 0.35, FIT_MARGIN 0.0.

| Metric | Baseline | Understanding | Gate |
|---|---|---|---|
| Request recall | 0.46 | 312/312 = 1.00 | 0.95 |
| Extra rows | 0.53 | 0/312 = 0.00 | <= 0.03 |
| Work started for nothing | 0.74 | 0/102 = 0.00 | <= 0.02 |
| Owner allowed | 0.92 | 304/306 = 0.99 | 0.90 |
| Scenes passing all runs | 0.47 | 111/112 = 0.99 | 0.85 |
| Latency p50 / p95 | 320 / 860 ms | 377 / 638 ms | p50 within +150 ms |

The one unstable scene: "Pull the signups for, uh, July, sorry, I mean August" goes to Pulse (marketing)
in two runs of three, Gauge (analytics) in one; Jev scores the two at 0.4 each.

This set was used to choose the cue list and the thresholds, so it overstates. The held-out set
(`understand_holdout.jsonl`, written by a separate agent that saw neither this set nor the code) is the
number to trust; it is run once and not tuned on.
