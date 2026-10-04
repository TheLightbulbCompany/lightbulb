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
| A helpful companion | 20 | 17 | 0.59 | 0.06 | 0.35 | 3 | 0.00 | 0.91 |
| B shared goal / split utterance | 28 | 14 | 0.71 | 0.00 | 0.29 | 14 | 0.07 | 0.91 |
| C reluctant / intermediary | 22 | 12 | 0.92 | 0.00 | 0.08 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.67 | 0.00 | 0.33 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 0.06 | 0.00 | 0.94 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.07 | 0.00 | 0.93 | 8 | 0.00 | 1.00 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.33 | 0.88 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.18 | 0.83 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.49 | 0.02 | 0.49 | 55 | 0.05 | 0.90 |
| **total** | 176 | 117 | 0.60 | 0.02 | 0.38 | 59 | 0.07 | 0.92 |

## all

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.59 | 0.06 | 0.35 | 3 | 0.00 | 0.91 |
| B shared goal / split utterance | 28 | 14 | 0.71 | 0.00 | 0.29 | 14 | 0.07 | 0.91 |
| C reluctant / intermediary | 22 | 12 | 0.92 | 0.00 | 0.08 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.67 | 0.00 | 0.33 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 0.06 | 0.00 | 0.94 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.07 | 0.00 | 0.93 | 8 | 0.00 | 1.00 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.33 | 0.88 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.18 | 0.83 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.49 | 0.02 | 0.49 | 55 | 0.05 | 0.90 |
| **total** | 176 | 117 | 0.60 | 0.02 | 0.38 | 59 | 0.07 | 0.92 |

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
| B / second-half / voice | 10 | 10 | 0.60 | 0.00 | 0.40 | 0 | – | 1.00 |
| B / we-goal / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / after-person / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / agent-named / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / dangling / voice | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| C / need-to-colleague / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| C / personal / voice | 5 | 0 | – | – | – | 5 | 0.00 | – |
| C / suggestion / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| D / answer-still-needed / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / answered / voice | 8 | 0 | – | – | – | 8 | 0.00 | – |
| D / floor-holder / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / hedged / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / other-goal-open / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / partial / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / reopened / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-goals-open / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-intent / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / ack-after-supply / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / elliptical / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| E / option-letter / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / other-human-supplies / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| E / relay-to-person / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / threaded / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / contradicts-agent / voice | 9 | 9 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-agree / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree-wrong / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-disagree / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree-fact / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / retracted / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / same-author / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / settled-by-human / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / tie-break / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / urgent-correction / voice | 2 | 2 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| H / follow-up / voice | 1 | 1 | 0.00 | 1.00 | 0.00 | 0 | – | 0.00 |
| H / interleaved / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / only-listener / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / quiet-room / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / burst / voice | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| R / dm / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / name-collision / voice | 4 | 0 | – | – | – | 4 | 0.50 | 0.00 |
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

## Misses (51)

| case | expected | got | how | P(who) |
|---|---|---|---|---|
| A02-voice | ['Engineering'] | none | none |  |
| A04-voice | ['Analytics'] | ['CEO'] | requests |  |
| A10-voice | ['Forge'] | none | none |  |
| A11-voice | ['Gauge'] | none | none |  |
| A16-voice | ['Finance'] | none | none |  |
| A19-voice | ['Ledger'] | none | none |  |
| A20-voice | ['Engineering'] | none | none |  |
| B01b-voice | ['Finance'] | none | none |  |
| B02b-voice | ['Engineering'] | none | none |  |
| B08b-voice | ['Engineering'] | none | none |  |
| B10b-voice | ['Friday'] | none | none |  |
| B18-voice | none | ['Friday'] | requests |  |
| C19-voice | ['Finance'] | none | none |  |
| D01-voice | ['Friday'] | none | none |  |
| D09-voice | ['Finance'] | none | none |  |
| D12-voice | ['Finance'] | none | none |  |
| D17-voice | ['Growth'] | none | none |  |
| E01-voice | ['Customer Operations'] | none | none |  |
| E02-voice | ['Finance'] | none | none |  |
| E03-voice | ['Forge'] | none | none |  |
| E04-voice | ['Gauge'] | none | none |  |
| E05-voice | ['Pulse'] | none | none |  |
| E06-voice | ['Ledger'] | none | none |  |
| E07-voice | ['Engineering'] | none | none |  |
| E08-voice | ['Analytics'] | none | none |  |
| E10-voice | ['Ledger'] | none | none |  |
| E11-voice | ['Forge'] | none | none |  |
| E12-voice | ['Gauge'] | none | none |  |
| E14-voice | ['Customer Operations'] | none | none |  |
| E15-voice | ['CEO'] | none | none |  |
| E16-voice | ['Analytics'] | none | none |  |
| E17-voice | ['Pulse'] | none | none |  |
| E18-voice | ['Engineering'] | none | none |  |
| E19-voice | ['Finance'] | none | none |  |
| F01-voice | ['Finance'] | none | none |  |
| F02-voice | ['Engineering'] | none | none |  |
| F03-voice | ['Friday'] | none | none |  |
| F06-voice | ['Finance'] | none | none |  |
| F07-voice | ['Gauge'] | none | none |  |
| F08-voice | ['Pulse'] | none | none |  |
| F14-voice | ['Customer Operations'] | none | none |  |
| F16-voice | ['Growth'] | none | none |  |
| F17-voice | ['Ledger'] | none | none |  |
| F18-voice | ['Pulse'] | none | none |  |
| F20-voice | ['Friday'] | none | none |  |
| F21-voice | ['Ledger'] | none | none |  |
| F22-voice | ['Friday'] | none | none |  |
| H15-voice | ['CEO'] | ['Finance'] | requests |  |
| R03-voice | none | ['Friday'] | requests |  |
| R05-voice | none | ['Friday'] | requests |  |
| T03-voice | none | ['CEO'] | requests |  |
