# map02 — Hermes Agent / Multi-Agent Model (pressure test)

Basis: `/Users/prasiddhaparthsarthy/Desktop/hermes-agent` @ `basis-v0.14.0-spec` (commit 4c46c35e).
All line numbers verified against this tree.

## BLUNT VERDICT

**Hermes has NO first-class multi-agent registry. A Hermes daemon is fundamentally ONE
agent / one persona / one SOUL.** There is nothing comparable to OpenClaw's
`agents.list[]` (multiple named, independently-addressable agents coexisting in one
daemon). Friday and Atlas cannot be two distinct registered agents in one Hermes
container. The ONLY way to "have two agents" in one daemon is to pass a different
`ephemeral_system_prompt` (the `instructions` field) on each `/v1/runs` or
`/v1/chat/completions` call — i.e. they are *simulated per-request personas over a
single shared agent runtime*, not real agents. The spec acknowledges this and builds
its whole Friday/Atlas plan on it; my reading confirms the gap is real and, if
anything, **deeper than the spec's framing implies** — the codebase has the literal
string `agent:main` hardcoded as the sole agent namespace, and the developers' own
comment calls "multiple agents running concurrently" a *future* feature.

## SIX BULLETS

- **No agent registry exists; the HTTP surface is OpenAI-shaped, not agent-shaped.**
  `gateway/platforms/api_server.py` (3524 lines) exposes `/v1/chat/completions`,
  `/v1/responses`, `/v1/runs`, `/v1/jobs`, `/v1/models` — and NOTHING resembling
  `GET/POST /v1/agents`. `grep agent_id|agent_registry|AgentRegistry|register_agent`
  returns ZERO hits in `api_server.py`. `/v1/models` (`_handle_models`, lines 943-962)
  returns exactly ONE model object whose `id` is `self._model_name`. Every request is
  served by a freshly-built `AIAgent` instance parameterized only by
  `ephemeral_system_prompt` + `session_id` (`_create_agent`, lines 851-912). There is
  no concept of "create an agent" as a persisted, addressable entity — "create" in
  Hermes means "start a *run*" (`POST /v1/runs` returns a `run_id`, not an `agent_id`).

- **"agent_id" does not exist as a routing dimension; session_id + ephemeral_system_prompt
  is the entire identity surface.** `_create_agent(ephemeral_system_prompt, session_id, ...)`
  (api_server.py:851) → `AIAgent(... ephemeral_system_prompt=..., session_id=..., model=model)`
  (lines 893-911). The request body's `model` field is NOT a router: in
  `_handle_chat_completions` it is read as `model_name = body.get("model", self._model_name)`
  (line 1138) and only **echoed back** in the response envelope (lines 1282, 2345, 2972) —
  it never selects a different persona, model, or agent. `init_agent` in
  `agent/agent_init.py` (the real constructor body, lines 74-208) takes a single `model`,
  a single `ephemeral_system_prompt`, a single `session_id`, a single `user_id` — it is
  by construction a one-agent object.

- **The session-key grammar hardcodes a SINGLE agent named "main".**
  `_parse_session_key` (`gateway/run.py:1017-1040`) documents the format as
  `agent:main:{platform}:{chat_type}:{chat_id}[:extra]` and only matches when
  `parts[0]=="agent" and parts[1]=="main"`. The dimensions that vary are *platform* and
  *chat*, never *agent identity*. There is no `agent:friday:` vs `agent:atlas:` namespace —
  the second segment is the literal constant `main`. Both `_running_agents`
  (`gateway/run.py:1240`) and `_agent_cache` (`gateway/run.py:1275`) are `Dict[str, ...]`
  keyed by this **session_key** (one entry per live conversation), NOT by any agent name.
  So even the daemon's internal "agent" bookkeeping is per-session, not per-named-agent.

- **The closest thing to "multiple agents" is PROFILES — but a profile is a single,
  process-wide, sticky-on-disk active config, not a coexistence model.**
  `_resolve_model_name` (api_server.py:689-706) can advertise the active *profile* name as
  the model id — so a Hermes daemon "is" one profile at a time. But profile activation is a
  single file: `~/.hermes/active_profile` (`hermes_cli/profiles.py:235-237, 1014-1042`);
  `set_active_profile` writes exactly one name; `get_active_profile` reads exactly one.
  Running Friday-as-profile-A AND Atlas-as-profile-B concurrently requires **separate
  HERMES_HOME directories = separate daemons = separate processes/ports**, which the
  developers themselves call a not-yet-built feature: `gateway/status.py:5-12` —
  *"separate HERMES_HOME directories naturally get separate PID files — a property that
  **will be useful when we add named profiles (multiple agents running concurrently** under
  distinct configurations)."* That is an explicit admission that one-daemon multi-agent
  does not exist today.

- **"Personalities" is a tone/styling toggle, not an agent.** `display.personality`
  (`hermes_cli/config.py:970`) defaults to `"kawaii"`; the user-extensible map
  `"personalities": {}` (config.py:1431) holds `{name: system_prompt}` entries. But
  selecting one is **daemon-global and one-at-a-time**: `/personality <name>` resolves the
  config entry and stores it into the daemon's *single* `_ephemeral_system_prompt`
  (per spec's verified cite `gateway/run.py:9650-9683`). It mutates shared state — it
  cannot host Friday AND Atlas simultaneously. So personalities are not a multi-agent
  primitive either; they are just a saved-string source for the same single
  `ephemeral_system_prompt` slot.

- **SOUL/identity is singular per home, and subagents (`delegate_task`) are ephemeral
  children, not peers.** Identity loads from ONE file: `get_hermes_home() / "SOUL.md"`
  (`agent/prompt_builder.py:1317`) — one soul per HERMES_HOME, full stop. The only
  "more than one agent at once" mechanism in the codebase is `delegate_task`
  (`tools/delegate_tool.py`): a parent (`_delegate_depth=0`, `agent/agent_init.py:375`)
  spawns transient child workers with `MAX_DEPTH=1` (delegate_tool.py:133, flat —
  grandchildren rejected), tracked in an in-memory `subagent_id -> record` map that
  "stays only [for the run]" (delegate_tool.py:153). These are short-lived,
  hierarchically-spawned, non-registered, non-addressable-from-outside helpers — the
  opposite of two persistent peer agents (Friday, Atlas) that an external orchestrator
  routes to by name. There is no API to address a subagent directly.

## CONTRAST WITH SPEC (skeptic's note)

The spec (lines 122-124, 313, 420) is **honest** that personalities are daemon-global and
that Friday/Atlas must be simulated via per-call `ephemeral_system_prompt`. It does NOT
oversell a registry. Where a founder should still push:

1. The spec frames per-call `ephemeral_system_prompt` as "both-agents-concurrent" and
   "independently addressable." That is true only in the weak sense that *two concurrent
   HTTP runs can carry two different persona strings*. There is no server-side notion that
   "Friday" and "Atlas" are durable entities — no list, no state, no per-agent config, no
   per-agent identity files, no per-agent cwd (confirmed at spec lines 147-159:
   `agent_workspace="hermes"` is a memory-scoping label, cwd is the daemon-global
   `TERMINAL_CWD`). Every notion of "which agent" lives **entirely in the Isol8 backend**,
   which must re-send the full persona on *every single request*. This is a materially
   different operational model from OpenClaw's `agents.list[]` and shifts real complexity
   (persona storage, routing, per-agent state, marketplace-agent identity) onto Isol8.

2. Consequence the spec underweights: anything that today relies on OpenClaw resolving an
   `agent_id` server-side (sessions list per agent, per-agent memory/cron/skills scoping,
   Paperclip's `adapterConfig.url`→agent binding, marketplace deploy creating a *new
   addressable agent*) has **no Hermes-side counterpart** and must be re-implemented as
   backend-held convention layered over a single shared daemon. The port's
   `seed_owner_agents` / `export_agent` / `list_sessions(agent_id)` contract (spec lines
   221-239) is papering over the fact that on the Hermes side `agent_id` is not a real key —
   it degrades to "which persona string + which session prefix the backend chooses to use."
