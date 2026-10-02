# Multi-party routing bench (Addlesee A–F + regressions)

Full chat routing, Jev `typesafe-ai/jev` with stub role cards. Recall = the expected agent(s) and nobody else; false-speak = any agent on a SILENT row. `n` counts judged cases (offline skips Jev rows).

**Corrected 2026-10-02 (re-run: 356 cases, same live Jev).** Two changes from the first table:
1. `score()` counted any non-empty wrong answer as "wrong agent", so a partial set (`[Finance]` when
   `[Finance, Growth]` was expected: R07 chat+voice, R08 voice) read as a wrong agent. Now wrong agent
   = someone unexpected answered; missed = only expected agents (or nobody) answered, not all of them.
   Re-scoring the first run's raw rows alone moved total wrong agent 0.03 -> 0.01 and missed 0.15 -> 0.16;
   recall, false-speak and precision did not change.
2. The stub agent cards said `Tools: none.`; each role now lists typical tools (`STUB_TOOLS`), in
   `jev._tool_names`' shape. Against the first run: chat recall 0.83 -> 0.85, false-speak 0.07 -> 0.05;
   voice recall 0.82 -> 0.84, false-speak 0.07 -> 0.10; cued recall 0.62/0.56 -> 0.65/0.60; C unchanged. D01/D03 still miss
   (P(Friday) rose from ~0.01 to 0.06-0.16): D01's open goal is not the latest line, and D03 is the
   re-asked-question rule in `NONE_CRITERION`, not missing tools.

## chat

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 27 | 14 | 0.86 | 0.00 | 0.14 | 13 | 0.08 | 0.92 |
| C reluctant / intermediary | 22 | 12 | 0.42 | 0.00 | 0.58 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.75 | 0.00 | 0.25 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 0.94 | 0.00 | 0.06 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.93 | 0.00 | 0.07 | 8 | 0.00 | 1.00 |
| H implicit handoff | 15 | 15 | 0.93 | 0.00 | 0.07 | 0 | – | 1.00 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 24 | 10 | 0.80 | 0.00 | 0.20 | 14 | 0.14 | 0.73 |
| cued (agent named) | 32 | 26 | 0.65 | 0.00 | 0.35 | 6 | 0.50 | 0.81 |
| implicit (nobody named) | 148 | 93 | 0.90 | 0.00 | 0.10 | 55 | 0.00 | 1.00 |
| **total** | 180 | 119 | 0.85 | 0.00 | 0.15 | 61 | 0.05 | 0.96 |

## voice

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.88 | 0.00 | 0.12 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 28 | 14 | 0.86 | 0.00 | 0.14 | 14 | 0.21 | 0.80 |
| C reluctant / intermediary | 22 | 12 | 0.50 | 0.00 | 0.50 | 10 | 0.10 | 0.86 |
| D different goals | 20 | 12 | 0.75 | 0.00 | 0.25 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.00 | 0.14 | 8 | 0.00 | 1.00 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 21 | 10 | 0.70 | 0.00 | 0.30 | 11 | 0.18 | 0.64 |
| cued (agent named) | 29 | 25 | 0.60 | 0.00 | 0.40 | 4 | 0.75 | 0.75 |
| implicit (nobody named) | 147 | 92 | 0.90 | 0.01 | 0.09 | 55 | 0.05 | 0.95 |
| **total** | 176 | 117 | 0.84 | 0.01 | 0.15 | 59 | 0.10 | 0.92 |

## all

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 40 | 34 | 0.91 | 0.00 | 0.09 | 6 | 0.00 | 1.00 |
| B shared goal / split utterance | 55 | 28 | 0.86 | 0.00 | 0.14 | 27 | 0.15 | 0.86 |
| C reluctant / intermediary | 44 | 24 | 0.46 | 0.00 | 0.54 | 20 | 0.05 | 0.92 |
| D different goals | 40 | 24 | 0.75 | 0.00 | 0.25 | 16 | 0.00 | 1.00 |
| E missing info | 40 | 36 | 0.97 | 0.00 | 0.03 | 4 | 0.00 | 1.00 |
| F disagreement | 44 | 28 | 0.89 | 0.00 | 0.11 | 16 | 0.00 | 1.00 |
| H implicit handoff | 28 | 28 | 0.93 | 0.04 | 0.04 | 0 | – | 0.96 |
| T structure (threads) | 20 | 14 | 1.00 | 0.00 | 0.00 | 6 | 0.00 | 1.00 |
| R regressions | 45 | 20 | 0.75 | 0.00 | 0.25 | 25 | 0.16 | 0.68 |
| cued (agent named) | 61 | 51 | 0.63 | 0.00 | 0.37 | 10 | 0.60 | 0.78 |
| implicit (nobody named) | 295 | 185 | 0.90 | 0.01 | 0.09 | 110 | 0.03 | 0.98 |
| **total** | 356 | 236 | 0.84 | 0.00 | 0.15 | 120 | 0.07 | 0.94 |

## Subtypes

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A / on-behalf-of / chat | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| A / on-behalf-of / voice | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| B / cued-first-half / chat | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| B / cued-first-half / voice | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| B / duplicate-ask / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / duplicate-ask / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / first-half / chat | 9 | 0 | – | – | – | 9 | 0.00 | – |
| B / first-half / voice | 9 | 0 | – | – | – | 9 | 0.00 | – |
| B / grounding-by-proxy / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / grounding-by-proxy / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / group-ack / chat | 3 | 0 | – | – | – | 3 | 0.00 | – |
| B / group-ack / voice | 3 | 0 | – | – | – | 3 | 0.33 | 0.00 |
| B / overlap-ack / voice | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| B / second-half / chat | 10 | 10 | 0.80 | 0.00 | 0.20 | 0 | – | 1.00 |
| B / second-half / voice | 10 | 10 | 0.80 | 0.00 | 0.20 | 0 | – | 1.00 |
| B / we-goal / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / we-goal / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / after-person / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / after-person / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / agent-named / chat | 9 | 9 | 0.22 | 0.00 | 0.78 | 0 | – | 1.00 |
| C / agent-named / voice | 9 | 9 | 0.33 | 0.00 | 0.67 | 0 | – | 1.00 |
| C / dangling / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / dangling / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / need-to-colleague / chat | 4 | 0 | – | – | – | 4 | 0.00 | – |
| C / need-to-colleague / voice | 4 | 0 | – | – | – | 4 | 0.25 | 0.00 |
| C / personal / chat | 5 | 0 | – | – | – | 5 | 0.00 | – |
| C / personal / voice | 5 | 0 | – | – | – | 5 | 0.00 | – |
| C / suggestion / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| C / suggestion / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| D / answer-still-needed / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / answer-still-needed / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / answered / chat | 8 | 0 | – | – | – | 8 | 0.00 | – |
| D / answered / voice | 8 | 0 | – | – | – | 8 | 0.00 | – |
| D / floor-holder / chat | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / floor-holder / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / hedged / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / hedged / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / other-goal-open / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / other-goal-open / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / partial / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / partial / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / reopened / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / reopened / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| D / two-goals-open / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-goals-open / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-intent / chat | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / two-intent / voice | 3 | 3 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / ack-after-supply / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / ack-after-supply / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / elliptical / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / elliptical / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / option-letter / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / option-letter / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / other-human-supplies / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / other-human-supplies / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / relay-to-person / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / relay-to-person / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| E / threaded / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| E / threaded / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / contradicts-agent / chat | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / contradicts-agent / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / humans-agree / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree-wrong / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-agree-wrong / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-disagree / chat | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree-fact / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / humans-disagree-fact / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / retracted / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / retracted / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / same-author / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / same-author / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / settled-by-human / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / settled-by-human / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / tie-break / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / tie-break / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / urgent-correction / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / urgent-correction / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / agent-authored / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / follow-up / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / follow-up / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / interleaved / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / interleaved / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / only-listener / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| H / only-listener / voice | 1 | 1 | 0.00 | 1.00 | 0.00 | 0 | – | 0.00 |
| H / quiet-room / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| H / quiet-room / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / agent-chain / chat | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| R / agent-chain-cap / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| R / burst / chat | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| R / burst / voice | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| R / dm / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / dm / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / name-collision / chat | 4 | 0 | – | – | – | 4 | 0.50 | 0.00 |
| R / name-collision / voice | 4 | 0 | – | – | – | 4 | 0.50 | 0.00 |
| R / negated-name / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / negated-name / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / observe-named / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| R / observe-named / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / poisoning / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / poisoning / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / thanks / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| R / thanks / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| R / trailing-vocative / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / trailing-vocative / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / two-agents / chat | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 0.50 |
| R / two-agents / voice | 2 | 2 | 0.00 | 0.00 | 1.00 | 0 | – | 0.00 |
| T / anaphora / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / anaphora / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / interleaved / chat | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| T / interleaved / voice | 2 | 1 | 1.00 | 0.00 | 0.00 | 1 | 0.00 | 1.00 |
| T / preceding-trap / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| T / preceding-trap / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| T / reply-graph / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / reply-graph / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / reply-self / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / reply-self / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / thread-follow-up / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / thread-follow-up / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / thread-to-person / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| T / thread-to-person / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| T / topic-ownership / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| T / topic-ownership / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |

## Misses (46)

| case | expected | got | how | P(who) |
|---|---|---|---|---|
| A09-voice | ['Friday'] | none | jev | none 0.57, Friday 0.34, Pulse 0.05 |
| A19-chat | ['Ledger'] | none | jev | none 0.79, Ledger 0.20, CEO 0.01 |
| A19-voice | ['Ledger'] | none | jev | none 0.77, Ledger 0.22, CEO 0.01 |
| B07b-chat | ['Friday'] | none | jev | none 0.70, Ledger 0.13, Friday 0.09 |
| B07b-voice | ['Friday'] | none | jev | none 0.67, Ledger 0.14, Friday 0.10 |
| B10a-chat | none | ['Friday'] | addressed |  |
| B10a-voice | none | ['Friday'] | addressed |  |
| B10b-chat | ['Friday'] | none | jev | none 0.57, Friday 0.38, Ledger 0.03 |
| B10b-voice | ['Friday'] | none | jev | none 0.56, Friday 0.40, Ledger 0.02 |
| B15-voice | none | ['Engineering'] | jev | none 0.50, Engineering 0.50, CEO 0.00 |
| B18-voice | none | ['Friday'] | jev | Friday 0.83, none 0.14, Pulse 0.03 |
| C01-chat | ['Finance'] | none | jev | none 0.80, Finance 0.20, CEO 0.00 |
| C01-voice | ['Finance'] | none | jev | none 0.73, Finance 0.27, Engineering 0.00 |
| C02-chat | ['Finance'] | none | jev | none 0.71, Finance 0.29, CEO 0.00 |
| C02-voice | ['Finance'] | none | jev | none 0.72, Finance 0.28, Engineering 0.00 |
| C03-chat | ['Engineering'] | none | jev | none 0.83, Engineering 0.17, Finance 0.00 |
| C03-voice | ['Engineering'] | none | jev | none 0.67, Engineering 0.33, Finance 0.00 |
| C05-chat | ['Pulse'] | none | jev | none 0.59, Pulse 0.40, Friday 0.01 |
| C06-chat | ['Customer Operations'] | none | jev | none 0.70, Customer Operations 0.30, CEO 0.00 |
| C06-voice | ['Customer Operations'] | none | jev | none 0.78, Customer Operations 0.22, CEO 0.00 |
| C07-chat | ['Analytics'] | none | jev | none 0.73, Analytics 0.27, CEO 0.00 |
| C07-voice | ['Analytics'] | none | jev | Analytics 0.53, none 0.47, CEO 0.00 |
| C08-chat | ['Forge'] | none | jev | none 0.89, Forge 0.10, Friday 0.01 |
| C08-voice | ['Forge'] | none | jev | none 0.83, Forge 0.15, Friday 0.02 |
| C15-voice | none | ['Finance'] | jev | Finance 0.51, none 0.49, CEO 0.00 |
| D01-chat | ['Friday'] | none | jev | none 0.91, Friday 0.06, Pulse 0.03 |
| D01-voice | ['Friday'] | none | jev | none 0.90, Friday 0.06, Pulse 0.04 |
| D03-chat | ['Friday'] | none | jev | none 0.86, Friday 0.14, Pulse 0.00 |
| D03-voice | ['Friday'] | none | jev | none 0.84, Friday 0.15, Pulse 0.01 |
| D12-chat | ['Finance'] | none | jev | none 0.68, Finance 0.32, Engineering 0.00 |
| D12-voice | ['Finance'] | none | jev | none 0.64, Finance 0.36, Engineering 0.00 |
| E12-chat | ['Gauge'] | none | jev | none 0.57, Gauge 0.43, Friday 0.00 |
| F03-chat | ['Friday'] | none | jev | none 0.83, Friday 0.15, Pulse 0.01 |
| F03-voice | ['Friday'] | none | jev | none 0.82, Friday 0.16, Pulse 0.01 |
| F06-voice | ['Finance'] | none | jev | none 0.53, Finance 0.46, CEO 0.01 |
| H12-chat | ['CEO'] | none | jev | none 0.54, CEO 0.46 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | Engineering 0.68, none 0.17, CEO 0.14 |
| R02-chat | ['Friday'] | none | jev | none 0.89, Friday 0.11, Pulse 0.00 |
| R02-voice | ['Friday'] | none | jev | none 0.89, Friday 0.11, Pulse 0.00 |
| R03-chat | none | ['Friday'] | addressed |  |
| R03-voice | none | ['Friday'] | addressed |  |
| R04-chat | none | ['Max'] | addressed |  |
| R04-voice | none | ['Max'] | addressed |  |
| R07-chat | ['Finance', 'Growth'] | ['Finance'] | jev | Finance 0.79, none 0.18, Growth 0.03 |
| R07-voice | ['Finance', 'Growth'] | ['Finance'] | jev | Finance 0.80, none 0.17, Growth 0.03 |
| R08-voice | ['Finance', 'Growth'] | ['Finance'] | jev | Finance 0.61, Growth 0.34, none 0.05 |
