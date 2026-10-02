# huddle-agent

The CEO huddle worker: a LiveKit Agents worker (agent name `lightbulb-huddle`)
that joins `huddle-{env}-{roomId}` as `agent-ceo`. Scaffolded from
[livekit-examples/agent-starter-python](https://github.com/livekit-examples/agent-starter-python) (MIT, see LICENSE).

## Run

    uv sync
    uv run pytest tests -q
    uv run python -m huddle_agent.agent dev

## Deploy

Built from `Dockerfile` (`uv.lock` must be committed); runs `python -m huddle_agent.agent start`.

The dispatch name is `lightbulb-huddle`: the Dockerfile sets `LIVEKIT_AGENT_NAME=lightbulb-huddle`,
which takes precedence over the (deprecated in livekit-agents 1.8.3) `agent_name=` kwarg in
`agent.py`. Keep the two equal; the backend dispatches by that name.

## Configuration

The worker reads (LiveKit Cloud agent secrets, set per project):

- `LIGHTBULB_API_URL` -- the backend, e.g. `https://api-dev.flickmeon.ai` (`/api/v1` optional)
- `LIGHTBULB_HUDDLE_SECRET` -- the value of the backend's `lightbulb/{env}/huddle_worker_secret`,
  sent as `X-Lightbulb-Huddle-Secret`
- `OPENAI_API_KEY` -- transcription and GPT-Live

LiveKit itself injects `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`.
Nothing a huddle says is stored.

## First deploy (once per LiveKit project)

`lk agent deploy` needs the `livekit.toml` (project subdomain + agent id) that `lk agent create`
writes, so the first deploy is by hand:

    cd apps/huddle-agent
    lk agent create --project lightbulb-dev \
      --secrets LIGHTBULB_API_URL=...,LIGHTBULB_HUDDLE_SECRET=...,OPENAI_API_KEY=...
    mv livekit.toml livekit.dev.toml   # commit it; prod is the same with livekit.prod.toml

`huddle-agent.yml` copies the env's file to `livekit.toml` before each deploy. Then set
`LIVEKIT_DEV_ENABLED=true`, and only after the worker runs set `LIVEKIT_URL` for the backend:
before that, every join dispatches a worker that never comes.
