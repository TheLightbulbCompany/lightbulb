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

One deployed agent serves every call (`agent.entrypoint`): a room named `phone-*` (the SIP dispatch rules'
room prefix) goes to the phone receptionist (`phone.entrypoint`), a job with `source: "meet"` to the meeting
agent (`meet.run`), anything else to the in-app huddle.

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
- `GOOGLE_API_KEY` -- Gemini Live, for Meet (`meet.py`) and phone calls (`phone.py`)

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

## Prod deploy (LiveKit Cloud agent `CA_HTFFgrTj24NH`, project `lightbulb-svx3a1rx`)

Prerequisites (checked 7 Nov 2026):

- Repo variable `LIVEKIT_PROD_ENABLED=true` and secrets `LIVEKIT_URL_PROD`, `LIVEKIT_API_KEY_PROD`,
  `LIVEKIT_API_SECRET_PROD`: all set (1 Oct).
- Agent secrets (`lk agent secrets` with `livekit.prod.toml` as `livekit.toml`): `LIGHTBULB_API_URL`
  (should be `https://api.thelightbulb.company`), `LIGHTBULB_HUDDLE_SECRET` (= `lightbulb/prod/huddle_worker_secret`),
  `OPENAI_API_KEY` are set. **`GOOGLE_API_KEY` is missing**: without it every Meet and phone call fails at
  session start. `lightbulb/prod/gemini_api_key` does not exist in Secrets Manager (only `lightbulb/dev/...`).
  Add it before the deploy (restarts the agent):

      lk agent update-secrets --secrets "GOOGLE_API_KEY=$(aws secretsmanager get-secret-value \
        --secret-id lightbulb/prod/gemini_api_key --profile isol8-admin --region us-east-1 \
        --query SecretString --output text)"

- The prod backend must serve `/internal/voice/*` (routers/voice_phone.py) and `/internal/voice/meet/*`.

Deploy (runs `test`, then `deploy-prod`, which builds this Dockerfile and `lk agent deploy`s it):

    gh workflow run huddle-agent.yml --ref main -R TheLightbulbCompany/lightbulb

Check: `lk agent list` shows a new Version and Deployed At; `lk agent logs` for the start.

## Prod phone line (plan, not done)

The project has one number, `+14842079294`, on the dev rule `lightbulb-phone-dev` (`SDR_tVyCQpx8AgUP`, agent
`lightbulb-phone-dev`, a Mac worker), and its `PHONE#` line row is in `lightbulb-dev-messaging` only.

1. A number for prod: buy a second LiveKit number, or move `+14842079294` to prod (dev then has no line).
2. The rule, targeting the deployed agent:

       echo '{"dispatch_rule": {"name": "lightbulb-phone-prod", "trunk_ids": ["<the number's PN_... id>"],
         "rule": {"dispatchRuleIndividual": {"roomPrefix": "phone-prod-"}},
         "room_config": {"agents": [{"agentName": "lightbulb-huddle"}]}}}' | lk sip dispatch create -

   (same shape as the dev rule in `lk sip dispatch list --json`; a moved number comes off `SDR_tVyCQpx8AgUP`
   first, since a number routes to one rule.)
3. The line row in `lightbulb-prod-messaging`: `assign_number` raises `not_available` outside dev, so either
   put `{pk: "PHONE#<e164>", sk: "LINE", ownerId: <org id>, ownerUserId: <user id>}` by hand, or make the
   number a per-env setting in `voice_onboarding.assign_number` so onboarding gives it.
