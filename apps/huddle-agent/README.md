# huddle-agent

The voice worker: one LiveKit Agents worker (agent name `lightbulb-huddle`) for phone calls (`phone.py`),
Google Meets (`meet.py`) and Live in a member's DM with the assistant (`owner.py`). Scaffolded from
[livekit-examples/agent-starter-python](https://github.com/livekit-examples/agent-starter-python) (MIT, see LICENSE).

## Run

    uv sync
    uv run pytest tests -q
    uv run python -m tests.replay.gate    # the rig's scenarios against the Meet gate, offline
    uv run python -m huddle_agent.agent dev

## Deploy

Built from `Dockerfile` (`uv.lock` must be committed); runs `python -m huddle_agent.agent start`.

The dispatch name is `lightbulb-huddle`: the Dockerfile sets `LIVEKIT_AGENT_NAME=lightbulb-huddle`,
which takes precedence over the (deprecated in livekit-agents 1.8.3) `agent_name=` kwarg in
`agent.py`. Keep the two equal; the backend dispatches by that name.

## Routing

One deployed agent serves every call (`agent.entrypoint`): a room named `phone-*` (the SIP dispatch rules'
room prefix) goes to the phone receptionist (`phone.entrypoint`), a job with `source: "meet"` to the meeting
agent (`meet.run`), `mode: "owner"` to owner Live (`owner.run`); anything else is logged and dropped.

## Configuration

The worker reads (LiveKit Cloud agent secrets, set per project):

- `LIGHTBULB_API_URL` -- the backend, e.g. `https://api-dev.flickmeon.ai` (`/api/v1` optional)
- `LIGHTBULB_HUDDLE_SECRET` -- the value of the backend's `lightbulb/{env}/huddle_worker_secret`,
  sent as `X-Lightbulb-Huddle-Secret`
- `OPENAI_API_KEY` -- transcription and the realtime model of Meet (`meet.py`) and phone calls
  (`phone.py`): `gpt-realtime-2.1-mini`, the phone greeting in `gpt-4o-mini-tts`
- `PHONE_ENGINE` / `MEET_ENGINE` -- `openai` (default) or `gemini`, the fallback, which needs `GOOGLE_API_KEY`
  (Gemini Live).
- `OWNER_ENGINE` -- owner mode (`owner.py`, Live in a member's DM with the assistant): `openai` (default,
  `gpt-realtime-2.1` on `OPENAI_API_KEY`) or `gemini` (Gemini Live on `GOOGLE_API_KEY`)

LiveKit itself injects `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`.
Nothing a huddle says is stored.

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
  `OPENAI_API_KEY` are set. `GOOGLE_API_KEY` is needed only for the `gemini` fallback
  (`PHONE_ENGINE` / `MEET_ENGINE`). `lightbulb/prod/gemini_api_key` does not exist in Secrets Manager (only `lightbulb/dev/...`).
  To use the fallback, add it (restarts the agent):

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
2. The rule, targeting the deployed agent: set `trunk_ids` in `scripts/phone_dispatch_rule.prod.json`, then run
   `scripts/phone_dispatch_rule.sh prod` (creates it, or replaces the rule of that name). By hand, it is:

       echo '{"dispatch_rule": {"name": "lightbulb-phone-prod", "trunk_ids": ["<the number's PN_... id>"],
         "rule": {"dispatchRuleIndividual": {"roomPrefix": "phone-prod-"}},
         "room_config": {"agents": [{"agentName": "lightbulb-huddle"}]}}}' | lk sip dispatch create -

   (same shape as the dev rule in `lk sip dispatch list --json`; a moved number comes off `SDR_tVyCQpx8AgUP`
   first, since a number routes to one rule.)
3. The line row in `lightbulb-prod-messaging`: `assign_number` raises `not_available` outside dev, so either
   put `{pk: "PHONE#<e164>", sk: "LINE", ownerId: <org id>, ownerUserId: <user id>}` by hand, or make the
   number a per-env setting in `voice_onboarding.assign_number` so onboarding gives it.
