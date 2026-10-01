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

Env: `LIVEKIT_URL`. Secrets (Secrets Manager `lightbulb/{env}/...`):
`livekit_api_key`, `livekit_api_secret`, `huddle_worker_secret`
(sent as `X-Lightbulb-Huddle-Secret` to the backend). Nothing a huddle says is stored.
