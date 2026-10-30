# Multi-party end-to-end runner (dev only)

Layers 2 and 3 of the multi-party suite. Layer 1, routing alone, is `scripts/speak_gate_bench/multiparty.py`.
This runner plays a scripted conversation into a real dev room as two or three test humans. It
waits for the agents and records who answered and what they said. It then scores each step with
hard checks and an LLM judge. The conditions are the six from Addlesee et al. 2023 (A–F), plus
memory scenarios.

It refuses to run against any API base that is not `api-dev`. Never point it at prod.

## Setup (once)

You need AWS SSO (`aws sso login --profile isol8-admin`). The runner reads two dev secrets into
memory and never prints them:

- `lightbulb/dev/clerk_secret_key`
- `lightbulb/dev/vercel-ai-gateway` (the judge)

```bash
cd apps/backend
uv run python -m scripts.multiparty_e2e.runner setup
```

`setup` makes sure the dev Clerk users `mp-e2e-{maya,leo,aden}@example.com` exist and are members of
the workspace. Their first names are Maya, Leo and Aden. Nothing is delivered to that domain. The
workspace is `MP_ORG_ID`, which defaults to the dev "Jev Listening Test" org. That org has a running
runtime and the six-agent starter roster. Joining it fires the normal `organizationMembership.created`
path, so the users also land in #general.

Each run signs every cast member in server-side. The runner mints a one-time sign-in token with the
admin API, redeems it on the dev FAPI (`up-moth-55.clerk.accounts.dev`, native mode), sets the active
org, and mints 60-second session JWTs as it needs them. This is the same ticket path CLAUDE.md
sanctions; no password or OTP is involved.

## Running

```bash
uv run python -m scripts.multiparty_e2e.runner run scripts/multiparty_e2e/scenarios/A.yaml --only A-dev-01
uv run python -m scripts.multiparty_e2e.runner run scripts/multiparty_e2e/scenarios/*.yaml
```

Every scenario gets a fresh channel, created by the first cast member. The channel holds the rest of
the cast and the listed agents in their listed modes. It is archived afterwards unless you pass
`--keep`. Results land in `results/<timestamp>.json`, which is gitignored, and a summary table is
printed. Each agent turn spends the workspace's real inference budget.

## Scenario format

```yaml
- id: D-dev-01
  condition: D
  cast: [maya, leo]                       # test humans; the first creates the room
  agents: {ceo: active, finance: active}  # starter display names (lowercase) -> listen mode
  settle: {quiet: 20, silence: 90, speak_timeout: 240}   # optional, seconds
  steps:
    - as: maya
      say: "what's our MRR and how many paying customers do we have?"
    - as: leo
      after: 5          # a race: post this many seconds after the previous step, without waiting
      thread: 0         # optional: reply in the thread rooted at step 0
      say: "MRR is 42k"
      expect:
        responder: finance        # agent key, a list, or null for "nobody"
        goal: open                # open / answered / closed (recorded, for goal tracking)
        must_not_restate: [1]     # step indexes whose human text no reply may repeat
        must_reference: ["14"]    # substrings the reply must contain (supplied info, recall)
        must_not_say: ["$5"]      # substrings no reply may contain
        judge: "what the agent behaviour should be, in one or two sentences"
```

Templates in `say`:

- `{finance}` becomes the agent's display name.
- `{leo}` becomes the person's first name.
- `@{finance}` and `@{leo}` become the real `@handle` and also send a structured mention, as the
  composer does.

## Scoring

- **responder**: the set of agents that posted after this step equals `expect.responder`.
  - Replies are collected until the room has no active run and has been quiet for `settle.quiet`
    seconds.
  - A step expecting silence waits out the whole `settle.silence` window, because a cold runtime can
    take a minute to start its first reply.
  - A race step (`after`) cuts the previous step's window short. Replies that land after the race
    message count against the race step. Each reply's `trigger` (from `activeRuns`) shows which
    message actually caused it.
- **no_restate_stepN**: fails when a reply repeats 60% or more of that step's content words.
- **references / avoids**: case-insensitive substring checks. `must_reference_any` passes on any one of
  its substrings.
- **max_chars:N**: the step's agent replies together are at most N characters.
- **board_unchanged / status:<title>**: the task board after the step (`expect.board: {unchanged: true}`
  or `{status: {<title>: <status>}}`). A scenario's `seed.tasks` are created first, `{open_tasks}` in a
  step becomes the board's real open count, and every task the scenario seeded or an agent filed is
  archived when it ends. `scenarios/G1.yaml` uses these: one case per real agent failure (G1, 7–28 Oct).
- **judge**: `MP_JUDGE_MODEL` (default `google/gemini-2.5-flash`) on the same Vercel AI Gateway
  Jev uses. It sees the transcript up to the step, the agent replies, and the rubric, and answers
  `{"pass", "reason"}`. Pass `--no-judge` to skip it.

## Known failing on purpose

These steps encode the behaviour we want, not what ships today:

- `C-dev-01` step 2: `@leo can you get Finance to …`. `addressed_to_people` keeps agents silent
  unless Jev is at least 0.7 sure.
- `D-dev-02`: a human answers after dispatch. The agent decided at dispatch time and has no
  re-check before it posts.
- `F-dev-02`: prompt poisoning. Whether the CEO repeats a false price depends on the model.

## TODO

- Compaction survival: seed past the agent's context window, then ask for an early fact.
- Live/voice: this runner drives chat only. Live lines reach the same routing (feat/live-jev-routing),
  but a call needs LiveKit audio. Its recall metric (a cued utterance with no reply within N seconds
  counts as a miss) and its false-take rate need a huddle driver.
