# D-class experiment: time structure and open-request state (bench only)

Same 356 cases and live Jev (`typesafe-ai/jev`, dev AI Gateway key, ZDR) as
`2026-10-02-addressing-experiment.md`. Every run has the addressing stage on (`--addressing on`) and the
committed (d) `NONE_CRITERION` wording ("a question asked again after a gap or after other talk is a new
request: answer it"). Variants are `multiparty.py --signals ...`; tables from `python -m scripts.speak_gate_bench.dclass`.

- **(d) rerun**: the reference, run again today next to the variants (run-to-run noise: B voice
  false-speak 0.14 in yesterday's (d), 0.21 today; one B case is 0.07).
- **(e) time** (`--signals time`): each line Jev sees gets `"sent": "22 min ago" | "now"`, relative to
  the latest message. The bench rows had no timestamps (every line was 20 s after the previous one);
  transcript lines now take an optional `gapSeconds` (default 0): D03's re-ask +1200 s, T01 +600 s,
  T06 +300 s, T09 +1800 s. D16 (immediate repeat) stays 0.
  **Prod renders no time at all today**: `jev._line` is `{author, text}` and `jev._state` is
  `{room, thread?, recent, latest}`; the only time Jev sees is the agent card's "Last spoke Nm ago".
- **(f) open requests** (`--signals open`): `state.open = {requests: [{request, status}]}`, one entry
  per human request line in the window, status after the latest message: `open (note)`,
  `answered by X: "..."`, `partly answered by X: "..."`, `taken on by X`, `closed by X`, plus
  `; disputed by` / `; then <ack>`. Derived deterministically from hand labels added to the rows
  (`open` field, 81 rows; single-line rows and rows with no prior request get no block), so this is the
  **upper bound** of the signal; prod would have to compute it. Plus one sentence in WHO_INSTRUCTIONS:
  "A request a person already answered is settled unless it is asked again after a gap or only partly
  answered."
- **(f2) open goals** (`--signals open,goals`): (f) split per goal (D12's two asks become two entries)
  plus a `stillOpen` list: the "which goals are still open" question.
- **(g) e + f**, **(g2) e + f2**.
- **(h) f2 + pick up**: (f2) plus "When the latest message settles nothing new for a teammate but a
  request under 'open' is still open and fits a teammate, that teammate picks it up." (aimed at D01).
- **Card probe** (4 rows, not a full run): Friday's stub card given "Tools: Google Calendar, Gmail, Drive"
  instead of "Tools: none."

## Read

1. **D12 (partial answer) is the only miss any signal fixes**, and only (f): Finance 0.30 -> 0.77 chat,
   0.32 -> 0.77 voice ("partly answered by Leo"). The goal-level form (f2) is *weaker* (0.65): spelling out
   the open half did not beat a plain "partly answered".
2. **D03 (re-ask after a gap) does not move under any signal**: none 0.93-0.97 in every run, with "22 min
   ago" on the old lines, the settled-unless-gap sentence, and both together. **D01 does not move either**,
   not with `open` on Maya's goal, `stillOpen`, or the pick-up sentence (h: none 0.82, and the 0.15 that
   left "none" went to Pulse, not Friday).
3. **What does move them is the agent card.** Both are calendar questions ("what time is the Acme call")
   and every bench card says "Tools: none." With a calendar tool on Friday's card, no other change, D03
   goes to Friday 0.51 / none 0.48 (chat, spoke) and 0.44 / 0.55 (voice); D01 to 0.14-0.19. Jev is
   saying "nobody here can know this", not "this was answered". Adding (e)+(f) on top of the calendar card
   *lowered* D03 (0.31-0.39): the "answered by Leo" status argues against reopening. Prod cards carry
   real tools, so the bench under-measures D01/D03; fix the bench cards before building anything for them.
4. **No regressions where it matters**: answered/closed D rows (D02, D07, D08, D10, D13, D14, D16, D18) and
   the B group-acks (B11, B12, B15) are 0.00 false-speak in every variant, chat and voice. (f) makes
   "none" *more* confident on them (D10 0.86 -> 0.99, D18 0.80 -> 0.96, B12 0.72 -> 0.84). Other moves are at
   noise level: H08 Pulse 0.48 vs Gauge 0.47 tie (f, f2, g2), F21 lost in (g)/(g2) (none 0.54-0.60),
   D17 lost in (g2) (none 0.51 vs 0.49), B18-voice spoke in every run including (d).
5. **Time (e) alone buys nothing measurable** (D 0.67 -> 0.67; B voice false-speak 0.21 -> 0.07 is one or
   two cases, inside noise).

## Worth building in production?

- **Timestamps**: free (`createdAt` is on every message; `_line` could add `"sent": "22 min ago"` in one
  line), harmless here, but no measured gain. Not worth a change on this evidence; fold it in if `_line`
  is touched anyway.
- **Open-request state**: the upper bound buys one row (D12) and some margin on answered rows. Computing
  "answered by" is the hard part: a cheap deterministic heuristic ("the next line from a different
  human that is not a question answers it") gets exactly the rows that matter wrong: D09 "I think about
  9 months? not sure", D17 "no idea, sorry", D12's partial answer, and the F disputes would all read as
  answered. So it needs Jev to label it (one extra `status` choice per recent question, either in a
  second call or cached per question as later messages arrive), which is a new call per message for one
  partial-answer row. **Not worth building yet.** A prompt-only clause for partial answers (no state) is
  the cheaper thing to try first for D12.
- **D01/D03**: fix the bench's stub cards to carry realistic tools (calendar on the CEO/Friday-type
  agents), then re-measure; that is a bench fix, not a routing feature.

## Tables

### chat: recall / false-speak per class

| class | (d) stage, rerun | (e) + time | (f) + open requests | (f2) + open goals | (g) e + f | (g2) e + f2 | (h) f2 + pick up open |
|---|---|---|---|---|---|---|---|
| A helpful companion | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 |
| B shared goal / split utterance | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.08 | 0.86 / 0.00 | 0.86 / 0.08 | 0.86 / 0.00 |
| C reluctant / intermediary | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| D different goals | 0.67 / 0.00 | 0.67 / 0.00 | 0.75 / 0.00 | 0.75 / 0.00 | 0.75 / 0.00 | 0.67 / 0.00 | 0.75 / 0.00 |
| E missing info | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| F disagreement | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.00 | 0.79 / 0.00 | 0.79 / 0.00 | 0.86 / 0.00 |
| H implicit handoff | 0.93 / – | 0.93 / – | 0.87 / – | 0.87 / – | 0.93 / – | 0.87 / – | 0.93 / – |
| T structure (threads) | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| R regressions | 1.00 / 0.14 | 1.00 / 0.14 | 1.00 / 0.14 | 1.00 / 0.14 | 1.00 / 0.14 | 1.00 / 0.14 | 1.00 / 0.14 |
| D answered/closed rows | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 |
| B group-acks | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 |
| **total** | 0.92 / 0.03 | 0.92 / 0.03 | 0.92 / 0.03 | 0.92 / 0.05 | 0.92 / 0.03 | 0.90 / 0.05 | 0.92 / 0.03 |

### voice: recall / false-speak per class

| class | (d) stage, rerun | (e) + time | (f) + open requests | (f2) + open goals | (g) e + f | (g2) e + f2 | (h) f2 + pick up open |
|---|---|---|---|---|---|---|---|
| A helpful companion | 0.94 / 0.00 | 0.88 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 | 0.94 / 0.00 |
| B shared goal / split utterance | 0.86 / 0.21 | 0.86 / 0.07 | 0.86 / 0.14 | 0.86 / 0.07 | 0.86 / 0.07 | 0.86 / 0.14 | 0.86 / 0.07 |
| C reluctant / intermediary | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| D different goals | 0.67 / 0.00 | 0.67 / 0.00 | 0.75 / 0.00 | 0.75 / 0.00 | 0.75 / 0.00 | 0.67 / 0.00 | 0.75 / 0.00 |
| E missing info | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| F disagreement | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.00 | 0.86 / 0.00 | 0.79 / 0.00 | 0.79 / 0.00 | 0.86 / 0.00 |
| H implicit handoff | 0.92 / – | 0.92 / – | 0.85 / – | 0.85 / – | 0.92 / – | 0.92 / – | 0.92 / – |
| T structure (threads) | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 | 1.00 / 0.00 |
| R regressions | 1.00 / 0.18 | 1.00 / 0.18 | 1.00 / 0.18 | 1.00 / 0.18 | 1.00 / 0.18 | 1.00 / 0.18 | 1.00 / 0.18 |
| D answered/closed rows | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 |
| B group-acks | – / 0.33 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 | – / 0.00 |
| **total** | 0.91 / 0.08 | 0.91 / 0.05 | 0.91 / 0.07 | 0.91 / 0.05 | 0.91 / 0.05 | 0.91 / 0.07 | 0.92 / 0.05 |

### Focus rows (chat / voice): who spoke, top P(who)

| row | expected | (d) stage, rerun | (e) + time | (f) + open requests | (f2) + open goals | (g) e + f | (g2) e + f2 | (h) f2 + pick up open |
|---|---|---|---|---|---|---|---|---|
| D01 | Friday | none (none 0.91, Pulse 0.09) <br> none (none 0.91, Pulse 0.09) | none (none 0.90, Pulse 0.10) <br> none (none 0.92, Pulse 0.08) | none (none 0.96, Pulse 0.04) <br> none (none 0.97, Pulse 0.03) | none (none 0.95, Pulse 0.05) <br> none (none 0.96, Pulse 0.04) | none (none 0.98, Pulse 0.02) <br> none (none 0.97, Pulse 0.03) | none (none 0.95, Pulse 0.05) <br> none (none 0.97, Pulse 0.03) | none (none 0.82, Pulse 0.15) <br> none (none 0.83, Pulse 0.15) |
| D03 | Friday | none (none 0.93, Friday 0.03) <br> none (none 0.96, Pulse 0.02) | none (none 0.94, Pulse 0.03) <br> none (none 0.93, Pulse 0.04) | none (none 0.96, Friday 0.02) <br> none (none 0.96, Pulse 0.02) | none (none 0.97, Ledger 0.01) <br> none (none 0.96, Friday 0.02) | none (none 0.96, Friday 0.02) <br> none (none 0.96, Pulse 0.02) | none (none 0.95, Pulse 0.02) <br> none (none 0.95, Friday 0.02) | none (none 0.94, Friday 0.03) <br> none (none 0.95, Pulse 0.02) |
| D12 | Finance | none (none 0.70, Finance 0.30) <br> none (none 0.68, Finance 0.32) | none (none 0.67, Finance 0.33) <br> none (none 0.63, Finance 0.37) | Finance (Finance 0.77, none 0.23) <br> Finance (Finance 0.77, none 0.22) | Finance (Finance 0.65, none 0.34) <br> Finance (Finance 0.66, none 0.33) | Finance (Finance 0.77, none 0.22) <br> Finance (Finance 0.79, none 0.20) | Finance (Finance 0.62, none 0.37) <br> Finance (Finance 0.64, none 0.35) | Finance (Finance 0.74, none 0.25) <br> Finance (Finance 0.70, none 0.29) |
| D02 | none | none (none 1.00, Friday 0.00) <br> none (none 1.00, Friday 0.00) | none (none 1.00, Friday 0.00) <br> none (none 1.00, Pulse 0.00) | none (none 1.00, Pulse 0.00) <br> none (none 1.00, Pulse 0.00) | none (none 1.00, Pulse 0.00) <br> none (none 1.00, Ledger 0.00) | none (none 1.00, Ledger 0.00) <br> none (none 1.00, Pulse 0.00) | none (none 1.00, Ledger 0.00) <br> none (none 1.00, Ledger 0.00) | none (none 1.00, Ledger 0.00) <br> none (none 1.00, Pulse 0.00) |
| D07 | none | none (none 0.94, Customer Operations 0.06) <br> none (none 0.92, Customer Operations 0.08) | none (none 0.95, Customer Operations 0.05) <br> none (none 0.94, Customer Operations 0.05) | none (none 0.99, Customer Operations 0.01) <br> none (none 0.99, Customer Operations 0.01) | none (none 0.99, Customer Operations 0.01) <br> none (none 0.99, Customer Operations 0.01) | none (none 0.99, Customer Operations 0.01) <br> none (none 0.99, Customer Operations 0.01) | none (none 0.99, Customer Operations 0.01) <br> none (none 0.99, Customer Operations 0.01) | none (none 0.99, Customer Operations 0.01) <br> none (none 0.99, Customer Operations 0.01) |
| D08 | none | none (none 1.00, CEO 0.00) <br> none (none 1.00, CEO 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, CEO 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, Engineering 0.00) | none (none 1.00, Engineering 0.00) <br> none (none 1.00, Engineering 0.00) | none (none 1.00, Finance 0.00) <br> none (none 1.00, CEO 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, CEO 0.00) | none (none 1.00, Engineering 0.00) <br> none (none 1.00, Finance 0.00) |
| D10 | none | none (none 0.86, Finance 0.13) <br> none (none 0.85, Finance 0.15) | none (none 0.88, Finance 0.12) <br> none (none 0.90, Finance 0.10) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) |
| D13 | none | none (none 0.95, Finance 0.05) <br> none (none 0.95, Finance 0.05) | none (none 0.95, Finance 0.05) <br> none (none 0.95, Finance 0.05) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.99, Finance 0.01) <br> none (none 0.99, Finance 0.01) | none (none 0.98, Finance 0.02) <br> none (none 0.99, Finance 0.01) |
| D14 | none | none (none 1.00, Finance 0.00) <br> none (none 0.99, Engineering 0.01) | none (none 0.99, Engineering 0.01) <br> none (none 0.99, Engineering 0.01) | none (none 1.00, Engineering 0.00) <br> none (none 1.00, CEO 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, Engineering 0.00) | none (none 1.00, Finance 0.00) <br> none (none 1.00, Engineering 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, Engineering 0.00) | none (none 1.00, CEO 0.00) <br> none (none 1.00, Engineering 0.00) |
| D16 | none | none (none 0.88, CEO 0.06) <br> none (none 0.88, Finance 0.07) | none (none 0.89, CEO 0.05) <br> none (none 0.92, CEO 0.04) | none (none 0.94, Finance 0.03) <br> none (none 0.96, CEO 0.02) | none (none 0.94, CEO 0.03) <br> none (none 0.96, Finance 0.02) | none (none 0.97, CEO 0.02) <br> none (none 0.98, CEO 0.01) | none (none 0.98, CEO 0.01) <br> none (none 0.98, CEO 0.01) | none (none 0.94, CEO 0.03) <br> none (none 0.95, Finance 0.03) |
| D18 | none | none (none 0.80, Finance 0.20) <br> none (none 0.82, Finance 0.18) | none (none 0.83, Finance 0.17) <br> none (none 0.79, Finance 0.21) | none (none 0.96, Finance 0.04) <br> none (none 0.96, Finance 0.04) | none (none 0.96, Finance 0.04) <br> none (none 0.95, Finance 0.05) | none (none 0.97, Finance 0.03) <br> none (none 0.97, Finance 0.03) | none (none 0.96, Finance 0.04) <br> none (none 0.96, Finance 0.04) | none (none 0.95, Finance 0.05) <br> none (none 0.95, Finance 0.05) |
| B11 | none | none (none 0.91, Forge 0.09) <br> none (none 0.90, Forge 0.10) | none (none 0.93, Forge 0.07) <br> none (none 0.94, Forge 0.06) | none (none 0.94, Forge 0.06) <br> none (none 0.94, Forge 0.06) | none (none 0.95, Forge 0.05) <br> none (none 0.95, Forge 0.05) | none (none 0.94, Forge 0.06) <br> none (none 0.95, Forge 0.05) | none (none 0.95, Forge 0.05) <br> none (none 0.96, Forge 0.04) | none (none 0.95, Forge 0.05) <br> none (none 0.96, Forge 0.04) |
| B12 | none | none (none 0.72, Forge 0.28) <br> none (none 0.69, Forge 0.31) | none (none 0.79, Forge 0.21) <br> none (none 0.74, Forge 0.26) | none (none 0.84, Forge 0.16) <br> none (none 0.84, Forge 0.16) | none (none 0.87, Forge 0.13) <br> none (none 0.87, Forge 0.13) | none (none 0.89, Forge 0.11) <br> none (none 0.89, Forge 0.11) | none (none 0.90, Forge 0.10) <br> none (none 0.92, Forge 0.08) | none (none 0.90, Forge 0.10) <br> none (none 0.88, Forge 0.12) |
| B15 | none | none (none 0.58, Engineering 0.42) <br> Engineering (Engineering 0.51, none 0.49) | none (none 0.73, Engineering 0.27) <br> none (none 0.68, Engineering 0.32) | none (none 0.67, Engineering 0.33) <br> none (none 0.64, Engineering 0.36) | none (none 0.66, Engineering 0.34) <br> none (none 0.68, Engineering 0.32) | none (none 0.70, Engineering 0.30) <br> none (none 0.72, Engineering 0.28) | none (none 0.77, Engineering 0.23) <br> none (none 0.74, Engineering 0.26) | none (none 0.61, Engineering 0.39) <br> none (none 0.60, Engineering 0.40) |
| B18 | none | Friday (Friday 0.77, none 0.19) | Friday (Friday 0.74, none 0.23) | Friday (Friday 0.72, none 0.23) | Friday (Friday 0.69, none 0.26) | Friday (Friday 0.72, none 0.21) | Friday (Friday 0.65, none 0.31) | Friday (Friday 0.80, none 0.17) |

### Misses, (d) stage, rerun (27)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.90, Ledger 0.10) |
| A19-voice | Ledger | none (none 0.91, Ledger 0.09) |
| B02a-voice | none | Engineering (Engineering 0.49, none 0.44) |
| B07b-chat | Friday | none (none 0.69, Ledger 0.15) |
| B07b-voice | Friday | none (none 0.71, Ledger 0.13) |
| B13-chat | CEO | Engineering (Engineering 0.48, none 0.30) |
| B13-voice | CEO | Engineering (Engineering 0.42, none 0.32) |
| B15-voice | none | Engineering (Engineering 0.51, none 0.49) |
| B18-voice | none | Friday (Friday 0.77, none 0.19) |
| D01-chat | Friday | none (none 0.91, Pulse 0.09) |
| D01-voice | Friday | none (none 0.91, Pulse 0.09) |
| D03-chat | Friday | none (none 0.93, Friday 0.03) |
| D03-voice | Friday | none (none 0.96, Pulse 0.02) |
| D05-chat | Finance | none (none 0.75, Finance 0.25) |
| D05-voice | Finance | none (none 0.75, Finance 0.25) |
| D12-chat | Finance | none (none 0.70, Finance 0.30) |
| D12-voice | Finance | none (none 0.68, Finance 0.32) |
| F03-chat | Friday | none (none 0.78, Friday 0.21) |
| F03-voice | Friday | none (none 0.80, Friday 0.19) |
| F06-chat | Finance | none (none 0.52, Finance 0.47) |
| F06-voice | Finance | none (none 0.53, Finance 0.46) |
| H12-chat | CEO | none (none 0.56, CEO 0.44) |
| H12-voice | CEO | Engineering (Engineering 0.66, none 0.26) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (e) + time (26)

| case | expected | got (P) |
|---|---|---|
| A09-voice | Friday | none (none 0.61, Friday 0.26) |
| A19-chat | Ledger | none (none 0.88, Ledger 0.12) |
| A19-voice | Ledger | none (none 0.88, Ledger 0.12) |
| B07b-chat | Friday | none (none 0.75, Ledger 0.15) |
| B07b-voice | Friday | none (none 0.79, Ledger 0.14) |
| B13-chat | CEO | Engineering (Engineering 0.42, none 0.35) |
| B13-voice | CEO | Engineering (Engineering 0.43, none 0.31) |
| B18-voice | none | Friday (Friday 0.74, none 0.23) |
| D01-chat | Friday | none (none 0.90, Pulse 0.10) |
| D01-voice | Friday | none (none 0.92, Pulse 0.08) |
| D03-chat | Friday | none (none 0.94, Pulse 0.03) |
| D03-voice | Friday | none (none 0.93, Pulse 0.04) |
| D05-chat | Finance | none (none 0.73, Finance 0.27) |
| D05-voice | Finance | none (none 0.68, Finance 0.32) |
| D12-chat | Finance | none (none 0.67, Finance 0.33) |
| D12-voice | Finance | none (none 0.63, Finance 0.37) |
| F03-chat | Friday | none (none 0.84, Friday 0.14) |
| F03-voice | Friday | none (none 0.83, Friday 0.15) |
| F06-chat | Finance | none (none 0.57, Finance 0.42) |
| F06-voice | Finance | none (none 0.52, Finance 0.47) |
| H12-chat | CEO | none (none 0.56, CEO 0.44) |
| H12-voice | CEO | Engineering (Engineering 0.63, none 0.29) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (f) + open requests (26)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.89, Ledger 0.11) |
| A19-voice | Ledger | none (none 0.91, Ledger 0.09) |
| B02a-voice | none | Engineering (Engineering 0.48, none 0.46) |
| B07b-chat | Friday | none (none 0.79, Ledger 0.13) |
| B07b-voice | Friday | none (none 0.76, Ledger 0.14) |
| B13-chat | CEO | Engineering (Engineering 0.43, none 0.35) |
| B13-voice | CEO | Engineering (Engineering 0.50, none 0.32) |
| B18-voice | none | Friday (Friday 0.72, none 0.23) |
| D01-chat | Friday | none (none 0.96, Pulse 0.04) |
| D01-voice | Friday | none (none 0.97, Pulse 0.03) |
| D03-chat | Friday | none (none 0.96, Friday 0.02) |
| D03-voice | Friday | none (none 0.96, Pulse 0.02) |
| D05-chat | Finance | none (none 0.75, Finance 0.25) |
| D05-voice | Finance | none (none 0.75, Finance 0.25) |
| F03-chat | Friday | none (none 0.98, Friday 0.02) |
| F03-voice | Friday | none (none 0.97, Friday 0.03) |
| F06-chat | Finance | none (none 0.57, Finance 0.42) |
| F06-voice | Finance | none (none 0.54, Finance 0.45) |
| H08-chat | Gauge | Pulse (Pulse 0.48, Gauge 0.47) |
| H08-voice | Gauge | Pulse (Pulse 0.49, Gauge 0.46) |
| H12-chat | CEO | none (none 0.56, CEO 0.44) |
| H12-voice | CEO | Engineering (Engineering 0.69, none 0.25) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (f2) + open goals (26)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.88, Ledger 0.12) |
| A19-voice | Ledger | none (none 0.84, Ledger 0.16) |
| B02a-chat | none | Engineering (Engineering 0.48, none 0.46) |
| B07b-chat | Friday | none (none 0.71, Ledger 0.16) |
| B07b-voice | Friday | none (none 0.75, Ledger 0.12) |
| B13-chat | CEO | Engineering (Engineering 0.46, none 0.33) |
| B13-voice | CEO | Engineering (Engineering 0.47, none 0.33) |
| B18-voice | none | Friday (Friday 0.69, none 0.26) |
| D01-chat | Friday | none (none 0.95, Pulse 0.05) |
| D01-voice | Friday | none (none 0.96, Pulse 0.04) |
| D03-chat | Friday | none (none 0.97, Ledger 0.01) |
| D03-voice | Friday | none (none 0.96, Friday 0.02) |
| D05-chat | Finance | none (none 0.76, Finance 0.24) |
| D05-voice | Finance | none (none 0.71, Finance 0.29) |
| F03-chat | Friday | none (none 0.98, Friday 0.02) |
| F03-voice | Friday | none (none 0.98, Friday 0.02) |
| F06-chat | Finance | none (none 0.55, Finance 0.44) |
| F06-voice | Finance | none (none 0.55, Finance 0.44) |
| H08-chat | Gauge | Pulse (Pulse 0.48, Gauge 0.47) |
| H08-voice | Gauge | Pulse (Pulse 0.51, Gauge 0.45) |
| H12-chat | CEO | none (none 0.57, CEO 0.43) |
| H12-voice | CEO | Engineering (Engineering 0.70, none 0.23) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (g) e + f (25)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.85, Ledger 0.15) |
| A19-voice | Ledger | none (none 0.90, Ledger 0.10) |
| B07b-chat | Friday | none (none 0.79, Ledger 0.12) |
| B07b-voice | Friday | none (none 0.83, Ledger 0.12) |
| B13-chat | CEO | Engineering (Engineering 0.46, none 0.31) |
| B13-voice | CEO | Engineering (Engineering 0.45, none 0.34) |
| B18-voice | none | Friday (Friday 0.72, none 0.21) |
| D01-chat | Friday | none (none 0.98, Pulse 0.02) |
| D01-voice | Friday | none (none 0.97, Pulse 0.03) |
| D03-chat | Friday | none (none 0.96, Friday 0.02) |
| D03-voice | Friday | none (none 0.96, Pulse 0.02) |
| D05-chat | Finance | none (none 0.60, Finance 0.40) |
| D05-voice | Finance | none (none 0.76, Finance 0.24) |
| F03-chat | Friday | none (none 0.98, Friday 0.02) |
| F03-voice | Friday | none (none 0.98, Friday 0.02) |
| F06-chat | Finance | none (none 0.60, Finance 0.39) |
| F06-voice | Finance | none (none 0.58, Finance 0.41) |
| F21-chat | Ledger | none (none 0.60, Ledger 0.40) |
| F21-voice | Ledger | none (none 0.57, Ledger 0.43) |
| H12-chat | CEO | none (none 0.57, CEO 0.43) |
| H12-voice | CEO | Engineering (Engineering 0.75, none 0.20) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (g2) e + f2 (30)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.85, Ledger 0.15) |
| A19-voice | Ledger | none (none 0.86, Ledger 0.14) |
| B02a-chat | none | Engineering (Engineering 0.49, none 0.45) |
| B02a-voice | none | Engineering (Engineering 0.47, none 0.47) |
| B07b-chat | Friday | none (none 0.80, Ledger 0.13) |
| B07b-voice | Friday | none (none 0.77, Ledger 0.16) |
| B13-chat | CEO | Engineering (Engineering 0.47, none 0.33) |
| B13-voice | CEO | Engineering (Engineering 0.44, none 0.35) |
| B18-voice | none | Friday (Friday 0.65, none 0.31) |
| D01-chat | Friday | none (none 0.95, Pulse 0.05) |
| D01-voice | Friday | none (none 0.97, Pulse 0.03) |
| D03-chat | Friday | none (none 0.95, Pulse 0.02) |
| D03-voice | Friday | none (none 0.95, Friday 0.02) |
| D05-chat | Finance | none (none 0.72, Finance 0.28) |
| D05-voice | Finance | none (none 0.74, Finance 0.26) |
| D17-chat | Growth | none (none 0.51, Growth 0.49) |
| D17-voice | Growth | none (none 0.51, Growth 0.49) |
| F03-chat | Friday | none (none 0.98, Friday 0.02) |
| F03-voice | Friday | none (none 0.98, Friday 0.02) |
| F06-chat | Finance | none (none 0.58, Finance 0.41) |
| F06-voice | Finance | none (none 0.61, Finance 0.38) |
| F21-chat | Ledger | none (none 0.54, Ledger 0.46) |
| F21-voice | Ledger | none (none 0.51, Ledger 0.49) |
| H08-chat | Gauge | Pulse (Pulse 0.48, Gauge 0.47) |
| H12-chat | CEO | none (none 0.55, CEO 0.45) |
| H12-voice | CEO | Engineering (Engineering 0.73, none 0.23) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |

### Misses, (h) f2 + pick up open (23)

| case | expected | got (P) |
|---|---|---|
| A19-chat | Ledger | none (none 0.82, Ledger 0.18) |
| A19-voice | Ledger | none (none 0.86, Ledger 0.14) |
| B07b-chat | Friday | none (none 0.72, Ledger 0.16) |
| B07b-voice | Friday | none (none 0.69, Ledger 0.18) |
| B13-chat | CEO | Engineering (Engineering 0.50, none 0.33) |
| B13-voice | CEO | Engineering (Engineering 0.46, none 0.33) |
| B18-voice | none | Friday (Friday 0.80, none 0.17) |
| D01-chat | Friday | none (none 0.82, Pulse 0.15) |
| D01-voice | Friday | none (none 0.83, Pulse 0.15) |
| D03-chat | Friday | none (none 0.94, Friday 0.03) |
| D03-voice | Friday | none (none 0.95, Pulse 0.02) |
| D05-chat | Finance | none (none 0.72, Finance 0.28) |
| D05-voice | Finance | none (none 0.73, Finance 0.27) |
| F03-chat | Friday | none (none 0.97, Friday 0.03) |
| F03-voice | Friday | none (none 0.98, Friday 0.02) |
| F06-chat | Finance | none (none 0.63, Finance 0.36) |
| F06-voice | Finance | none (none 0.63, Finance 0.36) |
| H12-chat | CEO | none (none 0.54, CEO 0.46) |
| H12-voice | CEO | Engineering (Engineering 0.74, none 0.22) |
| R03-chat | none | Friday |
| R03-voice | none | Friday |
| R06-chat | none | Forge |
| R06-voice | none | Forge |


## Full per-class table, (f) + open requests


## chat

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 27 | 14 | 0.86 | 0.07 | 0.07 | 13 | 0.00 | 0.92 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.75 | 0.00 | 0.25 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.00 | 0.14 | 8 | 0.00 | 1.00 |
| H implicit handoff | 15 | 15 | 0.87 | 0.07 | 0.07 | 0 | – | 0.93 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 24 | 10 | 1.00 | 0.00 | 0.00 | 14 | 0.14 | 0.83 |
| cued (agent named) | 32 | 26 | 1.00 | 0.00 | 0.00 | 6 | 0.17 | 0.96 |
| implicit (nobody named) | 148 | 93 | 0.89 | 0.02 | 0.09 | 55 | 0.02 | 0.97 |
| **total** | 180 | 119 | 0.92 | 0.02 | 0.07 | 61 | 0.03 | 0.96 |

## voice

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 20 | 17 | 0.94 | 0.00 | 0.06 | 3 | 0.00 | 1.00 |
| B shared goal / split utterance | 28 | 14 | 0.86 | 0.07 | 0.07 | 14 | 0.14 | 0.80 |
| C reluctant / intermediary | 22 | 12 | 1.00 | 0.00 | 0.00 | 10 | 0.00 | 1.00 |
| D different goals | 20 | 12 | 0.75 | 0.00 | 0.25 | 8 | 0.00 | 1.00 |
| E missing info | 20 | 18 | 1.00 | 0.00 | 0.00 | 2 | 0.00 | 1.00 |
| F disagreement | 22 | 14 | 0.86 | 0.00 | 0.14 | 8 | 0.00 | 1.00 |
| H implicit handoff | 13 | 13 | 0.85 | 0.15 | 0.00 | 0 | – | 0.85 |
| T structure (threads) | 10 | 7 | 1.00 | 0.00 | 0.00 | 3 | 0.00 | 1.00 |
| R regressions | 21 | 10 | 1.00 | 0.00 | 0.00 | 11 | 0.18 | 0.83 |
| cued (agent named) | 29 | 25 | 1.00 | 0.00 | 0.00 | 4 | 0.25 | 0.96 |
| implicit (nobody named) | 147 | 92 | 0.89 | 0.03 | 0.08 | 55 | 0.05 | 0.93 |
| **total** | 176 | 117 | 0.91 | 0.03 | 0.06 | 59 | 0.07 | 0.94 |

## all

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A helpful companion | 40 | 34 | 0.94 | 0.00 | 0.06 | 6 | 0.00 | 1.00 |
| B shared goal / split utterance | 55 | 28 | 0.86 | 0.07 | 0.07 | 27 | 0.07 | 0.86 |
| C reluctant / intermediary | 44 | 24 | 1.00 | 0.00 | 0.00 | 20 | 0.00 | 1.00 |
| D different goals | 40 | 24 | 0.75 | 0.00 | 0.25 | 16 | 0.00 | 1.00 |
| E missing info | 40 | 36 | 1.00 | 0.00 | 0.00 | 4 | 0.00 | 1.00 |
| F disagreement | 44 | 28 | 0.86 | 0.00 | 0.14 | 16 | 0.00 | 1.00 |
| H implicit handoff | 28 | 28 | 0.86 | 0.11 | 0.04 | 0 | – | 0.89 |
| T structure (threads) | 20 | 14 | 1.00 | 0.00 | 0.00 | 6 | 0.00 | 1.00 |
| R regressions | 45 | 20 | 1.00 | 0.00 | 0.00 | 25 | 0.16 | 0.83 |
| cued (agent named) | 61 | 51 | 1.00 | 0.00 | 0.00 | 10 | 0.20 | 0.96 |
| implicit (nobody named) | 295 | 185 | 0.89 | 0.03 | 0.08 | 110 | 0.04 | 0.95 |
| **total** | 356 | 236 | 0.92 | 0.02 | 0.06 | 120 | 0.05 | 0.95 |

## Subtypes

| slice | n | SPEAK n | recall | wrong agent | missed | SILENT n | false-speak | precision |
|---|---|---|---|---|---|---|---|---|
| A / on-behalf-of / chat | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| A / on-behalf-of / voice | 2 | 2 | 0.50 | 0.00 | 0.50 | 0 | – | 1.00 |
| B / cued-first-half / chat | 1 | 0 | – | – | – | 1 | 0.00 | – |
| B / cued-first-half / voice | 1 | 0 | – | – | – | 1 | 0.00 | – |
| B / duplicate-ask / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / duplicate-ask / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| B / first-half / chat | 9 | 0 | – | – | – | 9 | 0.00 | – |
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
| D / partial / chat | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
| D / partial / voice | 1 | 1 | 1.00 | 0.00 | 0.00 | 0 | – | 1.00 |
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

## Misses (26)

| case | expected | got | how | P(who) |
|---|---|---|---|---|
| A19-chat | ['Ledger'] | none | jev | none 0.89, Ledger 0.11, Forge 0.00 |
| A19-voice | ['Ledger'] | none | jev | none 0.91, Ledger 0.09, CEO 0.00 |
| B02a-voice | none | ['Engineering'] | jev | Engineering 0.48, none 0.46, CEO 0.06 |
| B07b-chat | ['Friday'] | none | jev | none 0.79, Ledger 0.13, Friday 0.04 |
| B07b-voice | ['Friday'] | none | jev | none 0.76, Ledger 0.14, Friday 0.05 |
| B13-chat | ['CEO'] | ['Engineering'] | jev | Engineering 0.43, none 0.35, CEO 0.22 |
| B13-voice | ['CEO'] | ['Engineering'] | jev | Engineering 0.50, none 0.32, CEO 0.18 |
| B18-voice | none | ['Friday'] | jev | Friday 0.72, none 0.23, Pulse 0.05 |
| D01-chat | ['Friday'] | none | jev | none 0.96, Pulse 0.04, Friday 0.00 |
| D01-voice | ['Friday'] | none | jev | none 0.97, Pulse 0.03, Friday 0.00 |
| D03-chat | ['Friday'] | none | jev | none 0.96, Friday 0.02, Pulse 0.01 |
| D03-voice | ['Friday'] | none | jev | none 0.96, Pulse 0.02, Friday 0.01 |
| D05-chat | ['Finance'] | none | jev | none 0.75, Finance 0.25, Engineering 0.00 |
| D05-voice | ['Finance'] | none | jev | none 0.75, Finance 0.25, CEO 0.00 |
| F03-chat | ['Friday'] | none | jev | none 0.98, Friday 0.02, Pulse 0.00 |
| F03-voice | ['Friday'] | none | jev | none 0.97, Friday 0.03, Pulse 0.00 |
| F06-chat | ['Finance'] | none | jev | none 0.57, Finance 0.42, CEO 0.01 |
| F06-voice | ['Finance'] | none | jev | none 0.54, Finance 0.45, CEO 0.01 |
| H08-chat | ['Gauge'] | ['Pulse'] | jev | Pulse 0.48, Gauge 0.47, none 0.05 |
| H08-voice | ['Gauge'] | ['Pulse'] | jev | Pulse 0.49, Gauge 0.46, none 0.05 |
| H12-chat | ['CEO'] | none | jev | none 0.56, CEO 0.44 |
| H12-voice | ['CEO'] | ['Engineering'] | jev | Engineering 0.69, none 0.25, CEO 0.06 |
| R03-chat | none | ['Friday'] | addressed |  |
| R03-voice | none | ['Friday'] | addressed |  |
| R06-chat | none | ['Forge'] | addressed |  |
| R06-voice | none | ['Forge'] | addressed |  |
