# huddle-agent

The huddle worker: a LiveKit Agents worker (agent name `lightbulb-huddle`)
that joins `huddle-{env}-{roomId}` as `agent-ceo`. Each AI agent speaks for itself: naming an agent
opens that agent's own GPT-Live session, and its requests run as ordinary Hermes runs on the agent's
room session key. Scaffolded from
[livekit-examples/agent-starter-python](https://github.com/livekit-examples/agent-starter-python) (MIT, see LICENSE).

## Run

    uv sync
    uv run pytest tests -q
    uv run python -m tests.replay all     # the live rig's scenarios against the controller, offline, in seconds
    uv run python -m huddle_agent.agent dev

## Deploy

Built from `Dockerfile` (`uv.lock` must be committed); runs `python -m huddle_agent.agent start`.

The dispatch name is `lightbulb-huddle`: the Dockerfile sets `LIVEKIT_AGENT_NAME=lightbulb-huddle`,
which takes precedence over the (deprecated in livekit-agents 1.8.3) `agent_name=` kwarg in
`agent.py`. Keep the two equal; the backend dispatches by that name.

## Wire contract

The worker is one LiveKit participant (`agent-ceo`) whatever agent is speaking.

- **Backend -> worker (C3):** data topic `call.frame`, sent server-side (no sender; anything a
  participant sends on it is dropped). Packet `{"events": [...]}`, each event a `voice.run.frame`
  (`payload: {roomId, runId, agentId, frame}`) or a `voice.notice` (`kind: renamed | voice_note_saved`).
- **Attributes (C4)** on `agent-ceo`:
  - `lightbulb.speaking_agent`: the floor-holder, from the moment its voice starts opening until it
    closes; `""` when none.
  - `lightbulb.agent.<id>.status`: `joining | speaking | listening | working | has_news | idle`.
  - `lightbulb.agent.<id>.working` (the agent's open request's task, else its running tool, <=60 chars) and `lightbulb.agent.<id>.naming`
    (`"1"` while a first-time-naming question is open).
  - `lightbulb.queue`: agent ids of a pending multi-name request, floor-holder first; else `""`.
  - `lightbulb.listening_to`: the pinned speaker's display name.
  - `""` deletes a key; a departed agent's keys are written `""`.
- **Worker -> people (C5):** data topic `live.notice`, JSON `{kind: renamed | voice_note_saved |
  ambiguous_name | voice_failed, agentId, ...}`. `huddle.error` is unchanged.
- **RPC (C6):** `talk_start` with payload `{"agentId": ...}`, or `""` for the last agent this caller
  summoned, else the CEO. An unknown agent id is ignored; tapping the floor-holder does nothing.
  `talk_end` is unchanged.
- **Audio (C7):** each agent session publishes a microphone track named `agent-<agentId>`. During a
  switch two can coexist, so clients filter by name.

The metering window (`/summon` .. `/end`) opens with the first voice and ends when no voice is open
or joining and no request of the call's request list is open (or at its deadline); a switch inside it takes no new `/summon`. Closing a voice, the window or the worker never
stops a run: a result that arrives while its agent is off the floor is held as `has_news`.

## Configuration

The worker reads (LiveKit Cloud agent secrets, set per project):

- `LIGHTBULB_API_URL` -- the backend, e.g. `https://api-dev.flickmeon.ai` (`/api/v1` optional)
- `LIGHTBULB_HUDDLE_SECRET` -- the value of the backend's `lightbulb/{env}/huddle_worker_secret`,
  sent as `X-Lightbulb-Huddle-Secret`
- `OPENAI_API_KEY` -- transcription and GPT-Live

LiveKit itself injects `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`.
Nothing a huddle says is stored.

- `HUDDLE_RECORD_DIR` -- test rigs only; never set on a deployed worker. With it the worker writes each
  call's inputs (what was said, what the backend answered, when) to that directory, so the call can be
  replayed offline (`tests/replay`).

## Deploys

The LiveKit plan allows one deployed agent: prod `lightbulb-huddle`. Dev workers run on the Mac
(`lk agent start`, or `uv run python -m huddle_agent.<module> dev`); nothing deploys them.

`lk agent deploy` needs the `livekit.toml` (project subdomain + agent id) that `lk agent create`
wrote; prod's is committed as `livekit.prod.toml`, and `huddle-agent.yml` (`deploy-prod`, manual,
`LIVEKIT_PROD_ENABLED=true`) copies it to `livekit.toml` before each deploy.

Time limits (cost caps): a phone call ends after `PHONE_MAX_S` (default 1200, 20 min), a meeting after
`MEET_MAX_S` (default 5400, 90 min). At the limit the assistant says a short goodbye and the session ends.
