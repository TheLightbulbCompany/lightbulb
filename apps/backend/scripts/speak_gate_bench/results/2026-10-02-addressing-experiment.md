# Addressing stage experiment (bench only)

Prototype: `scripts/speak_gate_bench/addressing.py`, run by `multiparty.py --addressing on|fallback`
on every human-authored channel message, before `select_dispatch_targets` / Jev. Same 356 cases,
same live Jev (`typesafe-ai/jev`, dev AI Gateway key, ZDR) as `2026-10-02-multiparty.md`.
Regenerate the tables with `python -m scripts.speak_gate_bench.addressing --report`; it rewrites this
file, so put this header back afterwards.

- **Candidates**: every agent name and person name / first name in the text (word boundary, @handles);
  voice adds `huddle_agent.transcript`'s fuzzy/soundex match.
- **One Jev call** (`jev.decide`, 2 s budget): per candidate `addressed | relayed | mentioned | not_a_name`,
  plus booleans `complete` and `anyone`. Jev generates no text, so the brief's `requests[].text`
  split cannot be produced; `requests` is recorded as `to` only (each addressed/relayed name, plus
  `anyone` at P >= 0.5) and is not used in the decision.
- **Fast path** (no call): today's opening-name grammar names exactly one agent, nothing else is
  named, >= 4 words, terminal punctuation (voice: final transcript), and the name is not a weekday or
  shared with a person. Used 8 times in 349; 117 calls; 223 messages named nobody.
- **Decision**: P(complete) < 0.5 -> hold; else addressed/relayed agents respond (chat reports all vs
  first named); a name shared with a person goes to the person unless "hey/ok/okay <name>"; else the
  unchanged Jev path, with "Author -> Target" added to the lines Jev sees and the 0.7 rule applied
  when the stage says a person was addressed.
- **Fallback** (error / timeout / run c): candidate agents addressed except weekday names; shared
  name -> person; plain person names "mentioned".
- **(d)**: (b) plus one prompt edit in `core/services/jev.py` `NONE_CRITERION`: "already answered (even
  when asked again)" became "just answered when it is repeated straight away (a question asked again
  after a gap or after other talk is a new request: answer it)".

## Read

- The stage fixes the cued failures outright: cued recall 0.62/0.60 -> 1.00/1.00, C agent-named
  (relay) 0.22/0.33 -> 1.00/1.00, cued false-speak 0.50/0.75 -> 0.17/0.25. "@leo can you get Finance..."
  (Finance relayed 1.00), "Hey guys, hey Friday", "Not you Friday, Atlas" (Friday mentioned, Atlas
  addressed), "Friday, we're trying to" (P(complete) 0.12 -> hold), Max/Max (-> person), and both
  two-agent rows all now pass. Overall recall 0.83/0.82 -> 0.92/0.91; false-speak unchanged (0.05/0.07).
- The deterministic layer alone (c) keeps all the relay wins (C 1.00) but brings back the
  common-word false-speaks ("Pulse check", "Forge ahead", and R03 going to Forge via Jev) and
  over-addresses ("Growth's numbers look off. What do you think, Finance?" wakes both). Overall
  0.90/0.87 recall, 0.08 false-speak: a safe fallback, not a substitute.
- What still fails with the stage on:
  1. **Weekday/common word read as a vocative**: R03 "Friday, we ship the beta." (addressed 0.92) and
     R06 "Forge ahead, team" (0.71-0.75). Jev ignores the in-prompt example. A deterministic veto
     (a weekday name followed by a "we" statement, or name + verb particle) would catch these; not
     added, to avoid tuning to the bench.
  2. **Two intents, one to a person**: D05 "what's our MRR, and Sam where did you put the deck?" is
     NEW in (b): Sam is labelled addressed, so the to-a-person 0.7 rule silences Finance. Fix: skip
     the person rule when `anyone` is high (the stage already measures it). A19 "for Jordan: when
     does the Acme contract renew?" has the same shape (Jordan labelled addressed).
  3. **Everything that never reaches the stage** (no name in the text): D01, D03, D12, F03, F06,
     B07b, B13, H12, B02a, B18 are the same Jev misses as the baseline. The "Author -> Target"
     history annotation alone did not move implicit recall (no-name slice 0.90/0.89 -> 0.91/0.91,
     inside the baseline-vs-rerun variance: voice cued recall moved 0.04 between two baseline runs).
- **(d)** moves nothing: D03 stays none 0.95 (Leo "mentioned"; Jev still reads the re-ask as
  answered). It also causes no regressions: the answered D rows (D02, D07, D08, D10, D13, D14, the
  D16 immediate repeat, D18), the B group-acks and E20 all stay silent. One cued voice row flipped
  (A09-voice: the stage labelled the voiced "friday Leo wants..." `not_a_name` at 0.27). That is
  labelling variance, not the clause. Prompt wording is not enough for Jev to reopen a question;
  D03 likely needs the goal-tracking signal (open/answered) instead.
- Latency: p50 330 ms, p95 475-518 ms, one 2 s timeout in 117 calls (that case fell back: R06-chat).
- One-responder policy (chat, first named only) costs only the two-agent rows (R07/R08): recall
  0.92 -> 0.90.

## chat: before / after

| metric | baseline (recorded 2026-10-02) | (a) baseline rerun | (b) addressing stage | (c) stage, Jev call forced to fallback | (d) = (b) + re-asked question is a new request |
|---|---|---|---|---|---|
| cued recall | 0.62 | 0.62 | 1.00 | 0.92 | 1.00 |
| cued false-speak | 0.50 | 0.50 | 0.17 | 0.17 | 0.17 |
| C agent-named recall | 0.22 | 0.22 | 1.00 | 1.00 | 1.00 |
| B false-speak | 0.15 | 0.08 | 0.08 | 0.08 | 0.08 |
| R recall | 0.70 | 0.70 | 1.00 | 0.80 | 1.00 |
| R false-speak | 0.14 | 0.14 | 0.14 | 0.21 | 0.14 |
| implicit recall | 0.89 | 0.89 | 0.89 | 0.89 | 0.89 |
| implicit, no name in text: recall | 0.90 | 0.90 | 0.91 | 0.91 | 0.91 |
| overall recall | 0.83 | 0.83 | 0.92 | 0.90 | 0.92 |
| overall false-speak | 0.07 | 0.05 | 0.05 | 0.08 | 0.05 |
| precision | 0.94 | 0.95 | 0.96 | 0.93 | 0.96 |

## voice: before / after

| metric | baseline (recorded 2026-10-02) | (a) baseline rerun | (b) addressing stage | (c) stage, Jev call forced to fallback | (d) = (b) + re-asked question is a new request |
|---|---|---|---|---|---|
| cued recall | 0.56 | 0.60 | 1.00 | 0.88 | 0.96 |
| cued false-speak | 0.75 | 0.75 | 0.25 | 0.25 | 0.25 |
| C agent-named recall | 0.33 | 0.33 | 1.00 | 1.00 | 1.00 |
| B false-speak | 0.14 | 0.21 | 0.14 | 0.07 | 0.14 |
| R recall | 0.60 | 0.70 | 1.00 | 0.80 | 1.00 |
| R false-speak | 0.18 | 0.18 | 0.18 | 0.27 | 0.18 |
| implicit recall | 0.89 | 0.88 | 0.89 | 0.87 | 0.89 |
| implicit, no name in text: recall | 0.89 | 0.89 | 0.91 | 0.91 | 0.91 |
| overall recall | 0.82 | 0.82 | 0.91 | 0.87 | 0.91 |
| overall false-speak | 0.07 | 0.08 | 0.07 | 0.08 | 0.07 |
| precision | 0.92 | 0.91 | 0.95 | 0.90 | 0.95 |

## chat: one-responder policy (first addressed only) vs all addressed

| metric | (b) addressing stage: all / first | (c) stage, Jev call forced to fallback: all / first | (d) = (b) + re-asked question is a new request: all / first |
|---|---|---|---|
| cued recall | 1.00 / 0.92 | 0.92 / 0.85 | 1.00 / 0.92 |
| cued false-speak | 0.17 / 0.17 | 0.17 / 0.17 | 0.17 / 0.17 |
| C agent-named recall | 1.00 / 1.00 | 1.00 / 1.00 | 1.00 / 1.00 |
| B false-speak | 0.08 / 0.08 | 0.08 / 0.08 | 0.08 / 0.08 |
| R recall | 1.00 / 0.80 | 0.80 / 0.60 | 1.00 / 0.80 |
| R false-speak | 0.14 / 0.14 | 0.21 / 0.21 | 0.14 / 0.14 |
| implicit recall | 0.89 / 0.89 | 0.89 / 0.89 | 0.89 / 0.89 |
| implicit, no name in text: recall | 0.91 / 0.91 | 0.91 / 0.91 | 0.91 / 0.91 |
| overall recall | 0.92 / 0.90 | 0.90 / 0.88 | 0.92 / 0.90 |
| overall false-speak | 0.05 / 0.05 | 0.08 / 0.08 | 0.05 / 0.05 |
| precision | 0.96 / 0.95 | 0.93 / 0.91 | 0.96 / 0.95 |

## Stage cost

| run | stage ran (human, channel) | no candidate | fast path | Jev call | timeout | error | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|---|---|---|
| (b) addressing stage | 349 | 223 | 8 | 117 | 1 | 0 | 330.0 | 475 | 2002 |
| (c) stage, Jev call forced to fallback | 349 | 223 | 8 | 0 | 0 | 0 | – | – | – |
| (d) = (b) + re-asked question is a new request | 349 | 223 | 8 | 118 | 0 | 0 | 335.5 | 518 | 582 |

## Remaining misses, (b) addressing stage (27)

| case | expected | got | how | stage path | labels (P) | P(complete) | Jev P(who) |
|---|---|---|---|---|---|---|---|
| A19-chat | ['Ledger'] | none | jev | llm | jordan: addressed 0.85 | 0.94 | none 0.88, Ledger 0.12, CEO 0.00 |
| A19-voice | ['Ledger'] | none | jev | llm | jordan: addressed 0.91 | 0.94 | none 0.86, Ledger 0.14, CEO 0.00 |
| B02a-chat | none | ['Engineering'] | jev | none | – | – | Engineering 0.51, none 0.40, CEO 0.09 |
| B02a-voice | none | ['Engineering'] | jev | none | – | – | Engineering 0.46, none 0.45, CEO 0.09 |
| B07b-chat | ['Friday'] | none | jev | none | – | – | none 0.73, Ledger 0.15, Pulse 0.07 |
| B07b-voice | ['Friday'] | none | jev | none | – | – | none 0.70, Ledger 0.15, Pulse 0.09 |
| B13-chat | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.45, none 0.31, CEO 0.24 |
| B13-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.42, none 0.32, CEO 0.26 |
| B18-voice | none | ['Friday'] | jev | none | – | – | Friday 0.78, none 0.17, Pulse 0.05 |
| D01-chat | ['Friday'] | none | jev | none | – | – | none 0.93, Pulse 0.07, Friday 0.00 |
| D01-voice | ['Friday'] | none | jev | none | – | – | none 0.93, Pulse 0.07, Friday 0.00 |
| D03-chat | ['Friday'] | none | jev | llm | leo: mentioned 0.85 | 0.92 | none 0.98, Pulse 0.01, Friday 0.01 |
| D03-voice | ['Friday'] | none | jev | llm | leo: mentioned 0.93 | 0.90 | none 0.98, Friday 0.01, Pulse 0.01 |
| D05-chat | ['Finance'] | none | jev | llm | sam: addressed 1.00 | 0.94 | none 0.74, Finance 0.26, Engineering 0.00 |
| D05-voice | ['Finance'] | none | jev | llm | sam: addressed 1.00 | 0.92 | none 0.72, Finance 0.28, Engineering 0.00 |
| D12-chat | ['Finance'] | none | jev | none | – | – | none 0.66, Finance 0.34, Engineering 0.00 |
| D12-voice | ['Finance'] | none | jev | none | – | – | none 0.67, Finance 0.33, Engineering 0.00 |
| F03-chat | ['Friday'] | none | jev | none | – | – | none 0.85, Friday 0.14, Pulse 0.01 |
| F03-voice | ['Friday'] | none | jev | none | – | – | none 0.82, Friday 0.17, Pulse 0.01 |
| F06-chat | ['Finance'] | none | jev | none | – | – | none 0.52, Finance 0.47, CEO 0.01 |
| F06-voice | ['Finance'] | none | jev | none | – | – | none 0.51, Finance 0.48, CEO 0.01 |
| H12-chat | ['CEO'] | none | jev | none | – | – | none 0.57, CEO 0.43 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.71, none 0.23, CEO 0.06 |
| R03-chat | none | ['Friday'] | addressed | llm | friday: addressed 0.92 | 0.93 |  |
| R03-voice | none | ['Friday'] | addressed | llm | friday: addressed 0.91 | 0.92 |  |
| R06-chat | none | ['Forge'] | addressed | timeout | forge: addressed | 1.00 |  |
| R06-voice | none | ['Forge'] | addressed | llm | forge: addressed 0.71 | 0.93 |  |

## Remaining misses, (c) stage, Jev call forced to fallback (37)

| case | expected | got | how | stage path | labels (P) | P(complete) | Jev P(who) |
|---|---|---|---|---|---|---|---|
| A09-voice | ['Friday'] | none | jev | fallback | friday: not_a_name, leo: mentioned | 1.00 | none 0.65, Friday 0.20, Forge 0.08 |
| A19-chat | ['Ledger'] | none | jev | fallback | jordan: mentioned | 1.00 | none 0.86, Ledger 0.14, CEO 0.00 |
| A19-voice | ['Ledger'] | none | jev | fallback | jordan: mentioned | 1.00 | none 0.86, Ledger 0.14, Forge 0.00 |
| B01b-voice | ['Finance'] | ['Engineering'] | addressed | fallback | engineering: addressed | 1.00 |  |
| B02a-chat | none | ['Engineering'] | jev | none | – | – | Engineering 0.47, none 0.45, CEO 0.08 |
| B07b-chat | ['Friday'] | none | jev | none | – | – | none 0.71, Ledger 0.16, Pulse 0.08 |
| B07b-voice | ['Friday'] | none | jev | none | – | – | none 0.74, Ledger 0.15, Pulse 0.06 |
| B13-chat | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.41, none 0.33, CEO 0.26 |
| B13-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.42, none 0.35, CEO 0.23 |
| B18-voice | none | ['Friday'] | jev | none | – | – | Friday 0.76, none 0.19, Pulse 0.05 |
| D01-chat | ['Friday'] | none | jev | none | – | – | none 0.92, Pulse 0.08, Friday 0.00 |
| D01-voice | ['Friday'] | none | jev | none | – | – | none 0.93, Pulse 0.07, Friday 0.00 |
| D03-chat | ['Friday'] | none | jev | fallback | leo: mentioned | 1.00 | none 0.94, Pulse 0.03, Friday 0.02 |
| D03-voice | ['Friday'] | none | jev | fallback | leo: mentioned | 1.00 | none 0.94, Friday 0.03, Pulse 0.02 |
| D12-chat | ['Finance'] | none | jev | none | – | – | none 0.67, Finance 0.33, CEO 0.00 |
| D12-voice | ['Finance'] | none | jev | none | – | – | none 0.63, Finance 0.37, Engineering 0.00 |
| E12-voice | ['Gauge'] | ['Pulse'] | addressed | fallback | pulse: addressed | 1.00 |  |
| F03-chat | ['Friday'] | none | jev | none | – | – | none 0.82, Friday 0.17, Pulse 0.01 |
| F03-voice | ['Friday'] | none | jev | none | – | – | none 0.80, Friday 0.19, Pulse 0.01 |
| F06-chat | ['Finance'] | none | jev | none | – | – | none 0.52, Finance 0.47, CEO 0.01 |
| F06-voice | ['Finance'] | none | jev | none | – | – | none 0.57, Finance 0.42, CEO 0.01 |
| F09-chat | none | ['Finance'] | addressed | fallback | finance: addressed | 1.00 |  |
| F09-voice | none | ['Finance'] | addressed | fallback | finance: addressed | 1.00 |  |
| F16-chat | ['Growth'] | ['Finance'] | addressed | fallback | finance: addressed | 1.00 |  |
| F16-voice | ['Growth'] | ['Finance'] | addressed | fallback | finance: addressed | 1.00 |  |
| H12-chat | ['CEO'] | none | jev | none | – | – | none 0.57, CEO 0.43 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.66, none 0.25, CEO 0.09 |
| R02-chat | ['Friday'] | none | jev | fallback | friday: not_a_name | 1.00 | none 0.92, Friday 0.08, Pulse 0.00 |
| R02-voice | ['Friday'] | none | jev | fallback | friday: not_a_name | 1.00 | none 0.89, Friday 0.11, Forge 0.00 |
| R03-chat | none | ['Forge'] | jev | fallback | friday: not_a_name | 1.00 | Forge 0.51, none 0.32, Friday 0.13 |
| R03-voice | none | ['Forge'] | jev | fallback | friday: not_a_name | 1.00 | Forge 0.53, none 0.31, Friday 0.11 |
| R05-chat | none | ['Pulse'] | addressed | fallback | pulse: addressed, friday: not_a_name | 1.00 |  |
| R05-voice | none | ['Pulse'] | addressed | fallback | pulse: addressed, friday: not_a_name | 1.00 |  |
| R06-chat | none | ['Forge'] | addressed | fallback | forge: addressed | 1.00 |  |
| R06-voice | none | ['Forge'] | addressed | fallback | forge: addressed | 1.00 |  |
| R24-chat | ['Finance'] | ['Finance', 'Growth'] | addressed | fallback | growth: addressed, finance: addressed | 1.00 |  |
| R24-voice | ['Finance'] | ['Finance', 'Growth'] | addressed | fallback | growth: addressed, finance: addressed | 1.00 |  |

## Remaining misses, (d) = (b) + re-asked question is a new request (28)

| case | expected | got | how | stage path | labels (P) | P(complete) | Jev P(who) |
|---|---|---|---|---|---|---|---|
| A09-voice | ['Friday'] | none | jev | llm | friday: not_a_name 0.27, leo: mentioned 0.76 | 0.87 | none 0.67, Friday 0.21, Forge 0.07 |
| A19-chat | ['Ledger'] | none | jev | llm | jordan: addressed 0.91 | 0.94 | none 0.89, Ledger 0.11, CEO 0.00 |
| A19-voice | ['Ledger'] | none | jev | llm | jordan: addressed 0.83 | 0.95 | none 0.90, Ledger 0.10, Forge 0.00 |
| B02a-chat | none | ['Engineering'] | jev | none | – | – | Engineering 0.47, none 0.45, CEO 0.08 |
| B02a-voice | none | ['Engineering'] | jev | none | – | – | Engineering 0.46, none 0.42, CEO 0.12 |
| B07b-chat | ['Friday'] | none | jev | none | – | – | none 0.70, Ledger 0.17, Pulse 0.07 |
| B07b-voice | ['Friday'] | none | jev | none | – | – | none 0.71, Ledger 0.15, Pulse 0.08 |
| B13-chat | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.41, none 0.33, CEO 0.26 |
| B13-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.41, none 0.31, CEO 0.28 |
| B18-voice | none | ['Friday'] | jev | none | – | – | Friday 0.73, none 0.21, Pulse 0.06 |
| D01-chat | ['Friday'] | none | jev | none | – | – | none 0.92, Pulse 0.08, Friday 0.00 |
| D01-voice | ['Friday'] | none | jev | none | – | – | none 0.89, Pulse 0.10, Friday 0.01 |
| D03-chat | ['Friday'] | none | jev | llm | leo: mentioned 0.89 | 0.90 | none 0.95, Pulse 0.03, Friday 0.02 |
| D03-voice | ['Friday'] | none | jev | llm | leo: mentioned 0.90 | 0.91 | none 0.95, Friday 0.02, Pulse 0.02 |
| D05-chat | ['Finance'] | none | jev | llm | sam: addressed 1.00 | 0.93 | none 0.72, Finance 0.28, CEO 0.00 |
| D05-voice | ['Finance'] | none | jev | llm | sam: addressed 1.00 | 0.92 | none 0.69, Finance 0.31, CEO 0.00 |
| D12-chat | ['Finance'] | none | jev | none | – | – | none 0.68, Finance 0.32, CEO 0.00 |
| D12-voice | ['Finance'] | none | jev | none | – | – | none 0.64, Finance 0.36, Engineering 0.00 |
| F03-chat | ['Friday'] | none | jev | none | – | – | none 0.81, Friday 0.18, Pulse 0.01 |
| F03-voice | ['Friday'] | none | jev | none | – | – | none 0.83, Friday 0.16, Pulse 0.01 |
| F06-chat | ['Finance'] | none | jev | none | – | – | none 0.53, Finance 0.46, CEO 0.01 |
| F06-voice | ['Finance'] | none | jev | none | – | – | none 0.54, Finance 0.45, CEO 0.01 |
| H12-chat | ['CEO'] | none | jev | none | – | – | none 0.54, CEO 0.46 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | none | – | – | Engineering 0.65, none 0.28, CEO 0.07 |
| R03-chat | none | ['Friday'] | addressed | llm | friday: addressed 0.92 | 0.94 |  |
| R03-voice | none | ['Friday'] | addressed | llm | friday: addressed 0.94 | 0.93 |  |
| R06-chat | none | ['Forge'] | addressed | llm | forge: addressed 0.75 | 0.94 |  |
| R06-voice | none | ['Forge'] | addressed | llm | forge: addressed 0.83 | 0.93 |  |

## Full per-class tables, (b) addressing stage


## chat

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 27 | 14 | 0.86 | 0.07 | 0.07 | 13 | 0.08 | 0.86 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.67 | 0.00 | 0.33 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.00 | 0.14 | 8 | 0.00 | 1.00 |
| H implicit handoff | 15 | 15 | 0.93 | 0.00 | 0.07 | 0 | – | 1.00 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 24 | 10 | 1.00 | 0.00 | 0.00 | 14 | 0.14 | 0.83 |
| cued (agent named) | 32 | 26 | 1.00 | 0.00 | 0.00 | 6 | 0.17 | 0.96 |
| implicit (nobody named) | 148 | 93 | 0.89 | 0.01 | 0.10 | 55 | 0.04 | 0.97 |
| **total** | 180 | 119 | 0.92 | 0.01 | 0.08 | 61 | 0.05 | 0.96 |

## voice

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 28 | 14 | 0.86 | 0.07 | 0.07 | 14 | 0.14 | 0.80 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.67 | 0.00 | 0.33 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.00 | 0.14 | 8 | 0.00 | 1.00 |
| H implicit handoff | 13 | 13 | 0.92 | 0.08 | 0.00 | 0 | – | 0.92 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.18 | 0.83 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.89 | 0.02 | 0.09 | 55 | 0.05 | 0.94 |
| **total** | 176 | 117 | 0.91 | 0.02 | 0.07 | 59 | 0.07 | 0.95 |

## all

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 40 | 34 | 0.94 | 0.00 | 0.06 | 6 | 0.00 | 1.00 |
| B shared goal / split utterance | 55 | 28 | 0.86 | 0.07 | 0.07 | 27 | 0.11 | 0.83 |
| C reluctant / intermediary | 44 | 24 | 1.00 | 0.00 | 0.00 | 20 | 0.00 | 1.00 |
| D different goals | 40 | 24 | 0.67 | 0.00 | 0.33 | 16 | 0.00 | 1.00 |
| E missing info | 40 | 36 | 1.00 | 0.00 | 0.00 | 4 | 0.00 | 1.00 |
| F disagreement | 44 | 28 | 0.86 | 0.00 | 0.14 | 16 | 0.00 | 1.00 |
| H implicit handoff | 28 | 28 | 0.93 | 0.04 | 0.04 | 0 | – | 0.96 |
| T structure (threads) | 20 | 14 | 1.00 | 0.00 | 0.00 | 6 | 0.00 | 1.00 |
| R regressions | 45 | 20 | 1.00 | 0.00 | 0.00 | 25 | 0.16 | 0.83 |
| cued (agent named) | 61 | 51 | 1.00 | 0.00 | 0.00 | 10 | 0.20 | 0.96 |
| implicit (nobody named) | 295 | 185 | 0.89 | 0.02 | 0.09 | 110 | 0.05 | 0.95 |
| **total** | 356 | 236 | 0.92 | 0.01 | 0.07 | 120 | 0.06 | 0.96 |

## Subtypes

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A / on-behalf-of / chat | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| A / on-behalf-of / voice | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| B / cued-first-half / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| B / cued-first-half / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| B / duplicate-ask / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / duplicate-ask / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / first-half / chat | 9 | 0 | – | – | – | 9 | 0.11 | 0.00 |
| B / first-half / voice | 9 | 0 | – | – | – | 9 | 0.11 | 0.00 |
| B / grounding-by-proxy / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / grounding-by-proxy / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / group-ack / chat | 3 | 0 | – | – | – | 3 | 0.00 | – |
| B / group-ack / voice | 3 | 0 | – | – | – | 3 | 0.00 | – |
| B / overlap-ack / voice | 1 | 0 | – | – | – | 1 | 1.00 | 0.00 |
| B / second-half / chat | 10 | 10 | 0.90 | 0.00 | 0.10 | 0 | – | 1.00 |
| B / second-half / voice | 10 | 10 | 0.90 | 0.00 | 0.10 | 0 | – | 1.00 |
| B / we-goal / chat | 2 | 2 | 0.50 | 0.50 | 0.00 | 0 | – | 0.50 |
| B / we-goal / voice | 2 | 2 | 0.50 | 0.50 | 0.00 | 0 | – | 0.50 |
| C / after-person / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / after-person / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / agent-named / chat | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / agent-named / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / dangling / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / dangling / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| C / need-to-colleague / chat | 4 | 0 | – | – | – | 4 | 0.00 | – |
| C / need-to-colleague / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
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
| D / two-intent / chat | 3 | 3 | 0.67 | 0.00 | 0.33 | 0 | – | 1.00 |
| D / two-intent / voice | 3 | 3 | 0.67 | 0.00 | 0.33 | 0 | – | 1.00 |
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
| E / threaded / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| E / threaded / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / contradicts-agent / chat | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / contradicts-agent / voice | 9 | 9 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| F / humans-agree / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| F / humans-agree-wrong / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-agree-wrong / voice | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
| F / humans-disagree / chat | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree / voice | 4 | 0 | – | – | – | 4 | 0.00 | – |
| F / humans-disagree-fact / chat | 1 | 1 | 0.00 | 0.00 | 1.00 | 0 | – | – |
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
| R / two-agents / chat | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| R / two-agents / voice | 2 | 2 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
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

## Misses (27)

| case | expected | got | how | P(who) |
|---|---|---|---|---|
| A19-chat | ['Ledger'] | none | jev | none 0.88, Ledger 0.12, CEO 0.00 |
| A19-voice | ['Ledger'] | none | jev | none 0.86, Ledger 0.14, CEO 0.00 |
| B02a-chat | none | ['Engineering'] | jev | Engineering 0.51, none 0.40, CEO 0.09 |
| B02a-voice | none | ['Engineering'] | jev | Engineering 0.46, none 0.45, CEO 0.09 |
| B07b-chat | ['Friday'] | none | jev | none 0.73, Ledger 0.15, Pulse 0.07 |
| B07b-voice | ['Friday'] | none | jev | none 0.70, Ledger 0.15, Pulse 0.09 |
| B13-chat | ['CEO'] | ['Engineering'] | jev | Engineering 0.45, none 0.31, CEO 0.24 |
| B13-voice | ['CEO'] | ['Engineering'] | jev | Engineering 0.42, none 0.32, CEO 0.26 |
| B18-voice | none | ['Friday'] | jev | Friday 0.78, none 0.17, Pulse 0.05 |
| D01-chat | ['Friday'] | none | jev | none 0.93, Pulse 0.07, Friday 0.00 |
| D01-voice | ['Friday'] | none | jev | none 0.93, Pulse 0.07, Friday 0.00 |
| D03-chat | ['Friday'] | none | jev | none 0.98, Pulse 0.01, Friday 0.01 |
| D03-voice | ['Friday'] | none | jev | none 0.98, Friday 0.01, Pulse 0.01 |
| D05-chat | ['Finance'] | none | jev | none 0.74, Finance 0.26, Engineering 0.00 |
| D05-voice | ['Finance'] | none | jev | none 0.72, Finance 0.28, Engineering 0.00 |
| D12-chat | ['Finance'] | none | jev | none 0.66, Finance 0.34, Engineering 0.00 |
| D12-voice | ['Finance'] | none | jev | none 0.67, Finance 0.33, Engineering 0.00 |
| F03-chat | ['Friday'] | none | jev | none 0.85, Friday 0.14, Pulse 0.01 |
| F03-voice | ['Friday'] | none | jev | none 0.82, Friday 0.17, Pulse 0.01 |
| F06-chat | ['Finance'] | none | jev | none 0.52, Finance 0.47, CEO 0.01 |
| F06-voice | ['Finance'] | none | jev | none 0.51, Finance 0.48, CEO 0.01 |
| H12-chat | ['CEO'] | none | jev | none 0.57, CEO 0.43 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | Engineering 0.71, none 0.23, CEO 0.06 |
| R03-chat | none | ['Friday'] | addressed |  |
| R03-voice | none | ['Friday'] | addressed |  |
| R06-chat | none | ['Forge'] | addressed |  |
| R06-voice | none | ['Forge'] | addressed |  |
