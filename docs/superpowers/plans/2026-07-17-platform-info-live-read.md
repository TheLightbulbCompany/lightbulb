# Implementation Plan — LIVE platform-info read (issue #1274)

Give the fleet agent a runtime read of the connectable **INTEGRATIONS** and **CHANNELS**
catalog — replacing the stale hardcoded list baked into the `lightbulb` skill. The agent's own
already-connected state is covered by the tools it already sees (see "Connected-state" below);
a per-owner connected-state endpoint is deliberately **deferred** to a focused follow-up.

## Auth decision (per Simplicity Mandate) — NO AUTH, one public route

**Ship a single, unauthenticated public GET.** The connectable catalog (integrations +
channels) is non-sensitive catalog data — equivalent to what any **authenticated** user sees
on the Integrations/Channels tabs (`connections.py:256` is `Depends(get_current_user)`, so it's
logged-in-visible, not anonymous — but the *content* is non-sensitive, which is what justifies a
no-auth route). No credential handling, no `OPENCLAW_HOOK_TOKEN`, no owner_id in the path, no
new secret, no new egress rule.

**Why not the per-owner connected-state route (dropped from v1):** the original plan added a
second `GET /platform-info/u/{owner_id}` route authenticated with `OPENCLAW_HOOK_TOKEN`. Review
killed it for three converging reasons, all of which point the same way:

1. **The agent already sees its connected integrations as tools.** Connected-integration state
   duplicates the toolset the agent already has. The motivating mom's-business incident (agent
   drafts an "Instagram bio" instead of connecting the integration) is about an **integration**,
   which the agent would already see once connected — the missing piece is knowing what's
   *connectable*, which the public catalog fully supplies.
2. **A baked skill has no owner_id at runtime.** `SKILL.md` is baked into the Docker image
   (`Dockerfile:418`), byte-identical for every owner, so it cannot carry `owner_id`. No
   `OWNER_ID`/`LIGHTBULB_OWNER_ID` env var is injected in `ecs_manager`, and the facts file
   `isol8-facts.json` carries plan/provider/owner_kind but **no owner_id** (`owner_facts.py:83-90`).
   `atlas_seed` only gets an owner-scoped URL because it is **templated per-owner at stage time**
   (`{{LIGHTBULB_BRIDGE_URL}}` substituted in `atlas_seed/__init__.py:112`) — the baked `lightbulb` skill
   has no such substitution. Building `GET /platform-info/u/<owner_id>` from a baked skill is not
   possible without new per-owner wiring the mandate warns against.
3. **Only connected-CHANNEL state was ever novel, and it carries a credential-leak hazard.**
   Channel connected-state lives in `channels.<provider>.accounts` in the owner's `openclaw.json`
   — the exact place bot tokens are stored (`config_patcher.py:712`). Returning it safely requires
   deriving *only* `bool(accounts)` and never echoing the account payload; that guard is easy to
   get wrong and not worth the risk for v1.

Connected-state is therefore a **deferred follow-up** (see "Deferred" section), not part of this
change. The agent's honest answer about what *it* has connected is "look at your own tools,"
which the SKILL edit says.

---

## Endpoint

New router `apps/backend/routers/platform_info.py`, mounted in `main.py` at
`prefix="/api/v1/platform-info"` (mirror the `agent_builder` registration at `main.py:812`).
**One route:**

### `GET /api/v1/platform-info` — public catalog (no auth)

- **Integrations:** `registry.all_providers()` (`core/services/credentials/registry.py:242`),
  **projected to a fixed field allowlist** (see below). MUST NOT `model_dump()` the `Provider`.
- **Channels:** the canonical `CHANNEL_PROVIDER_IDS` constant (`core/containers/config.py:44`),
  the single source of truth for channel keys. Display name derived from the key
  (`key.title()`) — **no hardcoded name map** (see drift note below).

### JSON response shape (fixed field allowlist)

```json
{
  "integrations": [
    { "key": "stripe", "display_name": "Stripe", "byo_required": false }
  ],
  "channels": [
    { "key": "telegram", "display_name": "Telegram" }
  ]
}
```

**Projection is mandatory, not incidental.** `registry.all_providers()` returns full `Provider`
models carrying internal wiring the public route must never disclose: `mcp_server.command/args/env`,
`SkillSpec.package` pins, `materializer` names, `ValidateSpec.url`/`token_prefix`,
`OAuthSpec.authorization_url/token_url/registration_url`, `AwsStsSpec` policy ARNs / cfn template
URLs (`registry.py:102-166`). None are per-owner secrets, but publishing them on a no-auth route is
gratuitous architecture disclosure. The router explicitly builds each dict from only:

- integrations: `key` (provider id), `display_name`, `byo_required`
- channels: `key`, `display_name`

A test asserts the response body contains **none** of `command`, `env`, `materializer`,
`token_url`, `package`, `policy_arn`.

### No `description` field (cut for v1)

The original plan added a `description: str | None` field to the `Provider` model plus 12
hand-authored `description:` lines in `providers.yaml`. **Cut.** The operator-mindset behavior
needs to know *what* is connectable, which `display_name` already supplies (`Stripe`, `GitHub`,
`Notion` are self-describing). A model change plus 12 drift-prone strings is avoidable v1
surface. If agents demonstrably need one-liners later, add `description` then. **No schema change
in this plan.**

---

## Container env wiring — the base URL

The container holds **no** platform-backend base URL today (grep of `core/containers/` for
`api-dev`/`LIGHTBULB_API`/`BACKEND_URL` is empty; `PAPERCLIP_API_URL` is Paperclip's, not reusable).
The agent needs a base URL to curl the public catalog, so inject one task-def env var — the
cheapest mechanism, and no auth since the route is public.

- In `provision_user_container` (`ecs_manager.py`), in the `environment_for_task` assembly block
  (starts ~`:1811`), unconditionally append:
  ```python
  environment_for_task.append(
      {"name": "LIGHTBULB_API_URL", "value": atlas_seed._api_base_url(settings.ENVIRONMENT)}
  )
  ```
- **Reuse `atlas_seed._api_base_url` directly** (import it) — do not write a second string
  builder. It already encodes the per-env convention: `prod`/empty → `https://api.flickmeon.ai`;
  else `https://api-{env}.flickmeon.ai` (dev → `api-dev.flickmeon.ai`). The router, the ecs_manager
  append, and the test all reference this same symbol — one name, no `_lightbulb_api_base_url` alias.
- **Reachability is already proven:** the Atlas `register-built-agent` flow is a
  container→`https://api-{env}.flickmeon.ai` HTTPS curl over the same NAT egress path today
  (`atlas_seed/files/TOOLS.md:21-30`). `api.flickmeon.ai` is the public ALB host. No new egress rule.

The agent constructs the catalog URL as `$LIGHTBULB_API_URL/api/v1/platform-info`. No owner_id, no
bearer token.

---

## SKILL.md edits — `apps/infra/openclaw/skills/lightbulb/SKILL.md`

Three changes. Keep the timeless behavioral rules already present (lines 61-72, 166-176); replace
the drift-prone hardcoded lists with a live-read instruction that matches the existing
roster/facts-file cadence.

### (1) Channels surface row — replace the hardcoded triplet (line 50)

BEFORE:
```
| Channels | Channels tab (Control Panel) | Telegram, Discord, Slack — chat with agents from outside the web app |
```
AFTER:
```
| Channels | Channels tab (Control Panel) | Chat with agents from outside the web app — read the live channel list from platform-info (see "Connectable services" below) |
```

### (2) Integrations section — live-read + operator-mindset + honest-fallback

Replace the top of the "## Integrations" section (lines 55-59) so it points at the live catalog
read instead of implying a fixed set.

BEFORE (lines 55-59):
```
## Integrations (connecting external services)

The **Integrations tab** (Control Panel, flickmeon.ai/chat) is where the owner
connects external services — each connection is an MCP server that Lightbulb AI
wires into this container's config automatically.
```
AFTER:
```
## Integrations and channels (connecting external services)

The **Integrations tab** (Control Panel, flickmeon.ai/chat) is where the owner
connects external services — each connection is an MCP server that Lightbulb AI
wires into this container's config automatically. **Channels** (chat surfaces
like Telegram/Discord/Slack) connect the same way, in the Channels tab.

**Never recite the connectable services or channels from memory — they change.**
Read the current catalog live:

- Connectable catalog (public, no auth): `GET $LIGHTBULB_API_URL/api/v1/platform-info`
  — returns the current integrations and channels you can offer to connect.
- What YOU already have connected: look at your own toolset. Connected
  integrations surface directly as tools; if a service's tools are present,
  it's connected.

**Operator mindset — connect and DO, don't just draft.** When a task needs an
external capability, first check the platform-info catalog: if the integration
is offered but its tools aren't in your toolset yet, tell the owner exactly
what to connect and where ("connect <service> in the Integrations tab, Control
Panel at flickmeon.ai/chat"), then do the real work once its tools appear. Do NOT
silently produce copy (e.g. "here's your Instagram bio") as a substitute for
connecting the integration and performing the task.

**Honest fallback — name what you can't do.** If a needed capability is NOT in
the platform-info catalog (no integration or channel offers it), say so
plainly: name the capability that's missing, then offer a manual/draft
fallback ("Lightbulb AI has no <X> integration today, so I can't post it directly —
here's the draft for you to publish yourself"). Never imply you did something
the platform can't do.
```

Keep the existing "What you observe / What you cannot do" bullets (lines 61-72) — timeless, and
already state that connected services surface as tools.

### (3) Quick answers — repoint the connect answer (lines 179-180)

BEFORE:
```
- **"How do I connect <service>?"** → Integrations tab, Control Panel,
  flickmeon.ai/chat. Its tools appear to you automatically once connected.
```
AFTER:
```
- **"How do I connect <service>?"** → Check the platform-info catalog for the
  current connectable services; if offered, connect it in the Integrations tab
  (or Channels tab), Control Panel, flickmeon.ai/chat. Its tools appear to you
  automatically once connected.
```

Leave line 45 (Control Panel tab enumeration) AS-IS — same class of drift but explicitly OUT of
scope for this issue.

---

## Delivery reality (state in PR)

- **Endpoint** (`routers/platform_info.py` + registration): ships on a **normal backend deploy**
  (`backend.yml` → ECR → ECS). The **endpoint itself** is reachable immediately fleet-wide.
- **`LIGHTBULB_API_URL` env var** (task-def) and **SKILL.md** (baked into the image at
  `apps/infra/openclaw/Dockerfile:418`): land on **REPROVISION**. The feature is **inert on the
  existing fleet until each container is reprovisioned** — existing containers lack both the
  `LIGHTBULB_API_URL` env (task-def, lands on reprovision) and the baked SKILL instruction telling
  them platform-info exists (image-baked, lands on reprovision), so they keep reciting the stale
  list until then. There is no backfill for a baked skill. New provisions get everything
  immediately.
- **After** a container is on the new skill, the drift-elimination promise holds: subsequent
  provider/channel additions reach that agent with **zero per-provider action** — no image
  rebuild, no reprovision, no skill edit. That is the whole point of the live read.

---

## Tests to add (`apps/backend/tests/…/test_platform_info.py`)

1. **Catalog is public** — `GET /api/v1/platform-info` with no auth returns 200, lists every
   `registry.all_providers()` integration + every `CHANNEL_PROVIDER_IDS` channel.
2. **New provider surfaces with no skill change** — append a fake provider (fixture
   `providers.yaml` or monkeypatch `all_providers`); assert it appears in the output. The
   drift-proof guarantee.
3. **New channel surfaces with no name-map edit** — add a fake key to `CHANNEL_PROVIDER_IDS`
   (monkeypatch); assert it appears with `display_name == key.title()`, no `KeyError`/500. Proves
   the "delete the hardcoded map" fix.
4. **No wiring disclosure** — assert the response body contains none of `command`, `env`,
   `materializer`, `token_url`, `package`, `policy_arn` (proves projection, not `model_dump()`).
5. **Env-URL derivation** — unit-assert `atlas_seed._api_base_url("dev") == "https://api-dev.flickmeon.ai"`
   and `atlas_seed._api_base_url("prod") == "https://api.flickmeon.ai"` (mirror the existing
   `atlas_seed` test if one exists).

Run targeted first (`uv run pytest tests/ -k platform_info`), full suite at the end.

---

## Concrete task list

1. `apps/backend/routers/platform_info.py` — new router: single public catalog route. Project
   integrations from `registry.all_providers()` to `{key, display_name, byo_required}`; channels
   from `CHANNEL_PROVIDER_IDS` to `{key, display_name: key.title()}`. No auth, no hook token, no
   EFS read.
2. `apps/backend/main.py` — `from routers import platform_info` (line 33 group) +
   `app.include_router(platform_info.router, prefix="/api/v1/platform-info", tags=["platform-info"])`.
3. `apps/backend/core/containers/ecs_manager.py` — in `environment_for_task` block (~:1811),
   append `LIGHTBULB_API_URL` derived via `atlas_seed._api_base_url(settings.ENVIRONMENT)` (import
   the symbol).
4. `apps/infra/openclaw/skills/lightbulb/SKILL.md` — the three edits above.
5. `apps/backend/tests/…/test_platform_info.py` — the five tests above.

No change to `registry.py` and no change to `providers.yaml` (description cut).

---

## Deferred — per-owner connected-state (NOT this PR)

Explicitly parked, not silently dropped. If connected-**channel** state proves load-bearing
(connected-**integration** state is already visible as tools), add a focused follow-up route
`GET /platform-info/u/{owner_id}` authenticated with `OPENCLAW_HOOK_TOKEN`, and at that time:

- **Wire owner_id first.** Inject the full owner-scoped URL as a per-container task-def env var
  in `ecs_manager` (which knows `user_id == owner_id`), e.g.
  `LIGHTBULB_PLATFORM_INFO_URL = https://api-{env}.flickmeon.ai/api/v1/platform-info/u/{owner_id}`, and
  reference `$LIGHTBULB_PLATFORM_INFO_URL` in the baked skill. The baked skill cannot construct an
  owner-scoped path on its own.
- **Integrations connected:** `integrations_repo.list_for_owner(owner_id)`, marking connected on
  `row.get("status") == "connected"` (mirror `connections.py:259`), **not** mere row presence — a
  row can exist mid-connect/error.
- **Channels connected — credential-leak guard (mandatory):** read `openclaw.json` via
  `read_openclaw_config_from_efs(owner_id)` and derive **only** `connected = bool(accounts)`.
  **Never** serialize or echo `channels.<provider>.accounts` or any value under it — those blocks
  hold live bot tokens (`config_patcher.py:712`). Mirror the safe precedent `channels.py:119-122`
  (reads `accounts.keys()`/agent_ids only, never values). Add a test asserting no bot-token /
  account-payload substring ever appears in the response body.
- **Auth note for the PR:** platform-info connected-state would be a third consumer of the
  un-rotated static `OPENCLAW_HOOK_TOKEN` (after `agent_builder` + `hooks_proxy`). It exposes only
  non-sensitive connected booleans, so a leaked token there cannot escalate beyond read-only
  connected-state disclosure of the owner's own account. Consider promoting `_require_hook_token`
  to a shared helper module at that point rather than a third verbatim copy.

## Out of scope (explicit)

- Control-Panel-tabs and plans/pricing in platform-info (YAGNI; the response shape is extensible
  so they *could* join later, but not now). Leave SKILL.md line 45 as-is.
- The dead `company.flickmeon.ai` Paperclip URL (#1276) and the whitespace bug (#1275).
- Any new auth mechanism, new secret, or new egress rule — none required for v1.
