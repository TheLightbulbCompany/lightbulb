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
