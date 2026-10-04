# Multi-party routing bench (Addlesee A–F + regressions)

Full chat routing, Jev `typesafe-ai/jev` with stub role cards. Recall = the expected agent(s) and nobody else; false-speak = any agent on a SILENT row. `n` counts judged cases (offline skips Jev rows).

## chat

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 0 | 0 | – | – | – | 0 | – | – |
| B shared goal / split utterance | 0 | 0 | – | – | – | 0 | – | – |
| C reluctant / intermediary | 0 | 0 | – | – | – | 0 | – | – |
| D different goals | 0 | 0 | – | – | – | 0 | – | – |
| E missing info | 0 | 0 | – | – | – | 0 | – | – |
| F disagreement | 0 | 0 | – | – | – | 0 | – | – |
| H implicit handoff | 0 | 0 | – | – | – | 0 | – | – |
| T structure (threads) | 0 | 0 | – | – | – | 0 | – | – |
| R regressions | 0 | 0 | – | – | – | 0 | – | – |
| cued (agent named) | 0 | 0 | – | – | – | 0 | – | – |
| implicit (nobody named) | 0 | 0 | – | – | – | 0 | – | – |
| **total** | 0 | 0 | – | – | – | 0 | – | – |

## voice

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 28 | 14 | 0.93 | 0.00 | 0.07 | 14 | 0.07 | 0.93 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.83 | 0.00 | 0.17 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.07 | 0.07 | 8 | 0.00 | 0.92 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.33 | 0.88 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.09 | 0.91 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.92 | 0.02 | 0.05 | 55 | 0.04 | 0.96 |
| **total** | 176 | 117 | 0.94 | 0.02 | 0.04 | 59 | 0.05 | 0.96 |

## all

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 28 | 14 | 0.93 | 0.00 | 0.07 | 14 | 0.07 | 0.93 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.83 | 0.00 | 0.17 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.07 | 0.07 | 8 | 0.00 | 0.92 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.33 | 0.88 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.09 | 0.91 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.92 | 0.02 | 0.05 | 55 | 0.04 | 0.96 |
| **total** | 176 | 117 | 0.94 | 0.02 | 0.04 | 59 | 0.05 | 0.96 |

## Subtypes

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A / on-behalf-of / voice | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| B / cued-first-half / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| B / duplicate-ask / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / first-half / voice | 9 | 0 | – | – | – | 9 | 0.00 | – |
| B / grounding-by-proxy / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / group-ack / voice | 3 | 0 | – | – | – | 3 | 0.00 | – |
| B / overlap-ack / voice | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| B / second-half / voice | 10 | 10 | 0.90 | 0.00 | 0.10 | 0 | – | 1.00 |
| B / we-goal / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / after-person / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / agent-named / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / dangling / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / need-to-colleague / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| C / personal / voice | 5 | 0 | – | – | – | 5 | 0.00 | – |
| C / suggestion / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| D / answer-still-needed / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / answered / voice | 8 | 0 | – | – | – | 8 | 0.00 | – |
| D / floor-holder / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / hedged / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / other-goal-open / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / partial / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / reopened / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-goals-open / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-intent / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / ack-after-supply / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / elliptical / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / option-letter / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / other-human-supplies / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / relay-to-person / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / threaded / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / contradicts-agent / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / humans-agree / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree-wrong / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-disagree / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree-fact / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / retracted / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / same-author / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / settled-by-human / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / tie-break / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / urgent-correction / voice | 2 | 2 | 0.50 | 0.50 | 0.00 | 0 | – | 0.50 |
| H / follow-up / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / interleaved / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / only-listener / voice | 1 | 1 | 0.00 | 1.00 | 0.00 | 0 | – | 0.00 |
| H / quiet-room / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / burst / voice | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| R / dm / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / name-collision / voice | 4 | 0 | – | – | – | 4 | 0.25 | 0.00 |
| R / negated-name / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / observe-named / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / poisoning / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / thanks / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| R / trailing-vocative / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / two-agents / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / anaphora / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / interleaved / voice | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| T / preceding-trap / voice | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| T / reply-graph / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / reply-self / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / thread-follow-up / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / thread-to-person / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| T / topic-ownership / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |

## Misses (10)

| case | expected | got | how | P(who) |
|---|---|---|---|---|
| A19-voice | ['Ledger'] | none | none |  |
| B09b-voice | ['Finance'] | none | hold |  |
| B18-voice | none | ['Friday'] | requests |  |
| D01-voice | ['Friday'] | none | none |  |
| D12-voice | ['Finance'] | none | none |  |
| F03-voice | ['Friday'] | none | none |  |
| F22-voice | ['Friday'] | ['Pulse'] | requests |  |
| H12-voice | ['CEO'] | ['Engineering'] | requests |  |
| R03-voice | none | ['Friday'] | requests |  |
| T03-voice | none | ['CEO'] | requests |  |
