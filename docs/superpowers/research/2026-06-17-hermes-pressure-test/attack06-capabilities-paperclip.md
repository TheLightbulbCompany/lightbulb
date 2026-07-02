# attack06 — Capabilities flags + the webhook.py Paperclip-ingress claim

**Basis:** Hermes v0.14.0 @ `4c46c35e`, `/Users/prasiddhaparthsarthy/Desktop/hermes-agent`. Every line below re-verified by reading the file this pass — not echoed from map08/map09. Adversarial read against `docs/superpowers/specs/2026-06-16-hermes-runtime-option-design.md` §4 (Capabilities, lines 250-257) + §10/workstream-7 ingress estimate.

---

## (a) Spec §4 Capabilities flags — verified row by row

| Capability | Spec says | Verdict | Evidence |
|---|---|---|---|
| `desktop_pairing` ❌ | correct | **CONFIRM** | The only provisionable backend↔container channel (`api_server.py`) authenticates with a **single shared bearer** `API_SERVER_KEY` (`api_server.py:647`, `_check_auth` `:742-760`, `hmac.compare_digest` `:756`). No caller identity, no device key. `gateway/pairing.py` is a chat-DM allowlist (`is_approved(platform, user_id)`), not device attestation — re-confirmed via map09 §5. |
| `device_exec_approval` ❌ | correct | **CONFIRM** | `/v1/runs/{run_id}/approval` exists (`api_server.py:14`) but is gated by the *same* single bearer; the WS RPC `approval.respond`/`sudo.respond` path has zero per-method auth (map09 §3-4). Hermes cannot distinguish "the paired operator device" from "anyone holding the key." No primitive to forward only trusted-device approvals. |
| `control_ui_proxy` ❌ | correct | **CONFIRM** | Isol8 target = `routers/control_ui_proxy.py` raw HTTP+WS proxy to `:18789`. Hermes' only operator console is `hermes_cli/web_server.py`, which **refuses non-loopback bind without `--insecure`** and uses an **ephemeral per-boot `_SESSION_TOKEN`** (`web_server.py:86`) that's never exposed for a remote backend to hold (map09 §1). No remote-drivable control plane. |
| `resumable_stream` ❌ | correct | **CONFIRM** | `/v1/runs/{id}/events` SSE queue is **popped in `finally` on any client disconnect** (`api_server.py:3217-3218`); intermediate deltas are lost, only terminal/incomplete snapshot survives in `state.db`. Reconnect = poll-for-terminal, exactly as spec Q1 states. |
| `per_agent_workspace` ❌ | correct | **CONFIRM** | Hermes is one `AIAgent` per process with one cwd/identity; agent context is `TERMINAL_CWD` + top-level SOUL.md/AGENTS.md (`agent/system_prompt.py:17`, `prompt_builder.py:1356-1357`). No per-agent cwd partition like OpenClaw's `agents/{uuid}/`. |
| `structured_run_context` ❌ | correct | **CONFIRM (and sharper than spec)** | Inbound payloads collapse to a **flat prompt string** via `_render_prompt` dot-path interpolation (`webhook.py:625-663`); `MessageEvent` carries `raw_message=payload` (`webhook.py:558`) but the agent runs on `event.text`, and `build_source` (`base.py:3567-3602`) carries only routing identity (chat_id/user_id/thread_id), **no domain metadata fields**. Paperclip's structured issue context (id, assignee, labels) survives only as interpolated wake-text. |

**Proposed additions — both justified:**

- **`durable_runs` ❌ — ADD.** All live-run state is **in-memory dicts**: `_run_streams`, `_active_run_agents`, `_active_run_tasks`, `_run_statuses`, `_run_approval_sessions` (`api_server.py:658-670`). A process restart loses every in-flight run; only terminal/incomplete *snapshots* persist to `state.db` (`ResponseStore`, `:657`, `_persist_*` `:1605-1666`). This matters to Isol8 because per-user containers restart on every config patch / image deploy / crash-loop recovery — an in-flight Paperclip-driven run is **silently destroyed** with no resume, worse than OpenClaw's session-on-EFS model. Brand-blind consumer: container-restart / Track-1/2 update logic + Paperclip orphan-recovery.

- **`multi_agent_registry` ❌ — ADD.** `_handle_runs` accepts `input/instructions/previous_response_id/conversation_history/session_id/model` and **no `agent_id`** (`api_server.py:2885-2941`). Hermes has *subagents* (ephemeral tool-spawned workers, `conversation_loop.py`), but no addressable persistent roster. Isol8's Friday+Atlas (two named, separately-addressable seed agents) has **no native Hermes home** — you'd map each to a separate Hermes container/process or fold them into one identity. This is arguably the single biggest modeling mismatch and the spec's §4 table omits it. Brand-blind consumer: `paperclip_provisioning.provision_org` (seeds 2 agents), `agents.list` RPC, marketplace deploy (new `agent_id` per listing).

---

## (b) The webhook.py Paperclip-ingress claim — CONFIRM the mechanism, NUANCE the "shrinks WS7 to near-zero" framing

**The interim report's core factual claims are TRUE and I verified each independently:**

- HMAC inbound: GitHub `X-Hub-Signature-256` / GitLab `X-Gitlab-Token` / generic `X-Webhook-Signature`, all `hmac.compare_digest`, reject-if-secret-but-no-sig (`webhook.py:590-619`). Auth-before-body, body-size cap, per-route rate-limit, delivery-id idempotency (`webhook.py:339-468`). **REAL.**
- Non-blocking 202: `asyncio.create_task(self.handle_message(event))` then `202 Accepted` (`webhook.py:571-584`). **REAL** — sidesteps the SSE-orphan problem entirely (server finishes headless and pushes).
- Push-back delivery: `send()` reads `_delivery_info[chat_id]` (`.get`, not pop, so interim status msgs don't consume it — `webhook.py:220-264,530-543`) and routes to `github_comment` (gh CLI subprocess, `:709-761`) or `_deliver_cross_platform` (`:763-806`). **REAL.**
- `deliver_only` zero-LLM rail (`webhook.py:174-181,476-526`) → maps to heartbeat/status pings. **REAL.**
- Plugin path: `Platform._missing_` mints dynamic enum members for registered plugins (`config.py:130-159`); `platform_registry.is_registered` gates non-builtin deliver targets (`webhook.py:248-258`, `platform_registry.py:205`); zero-core-change plugin model with bundled `teams/`+`google_chat/` examples (`ADDING_A_PLATFORM.md:5-58`). **REAL.**

**But the end-to-end "comment back into Paperclip" path does NOT close for free — three concrete cracks:**

1. **There is no `deliver: http_post` / arbitrary-URL delivery type.** The only built-in pushes are `github_comment` (hardwired `gh pr comment` subprocess, `webhook.py:726-736`) and `_deliver_cross_platform`, which requires `self.gateway_runner.adapters.get(target_platform)` to return a **live, already-connected adapter** (`webhook.py:780-785`). Paperclip is neither GitHub nor a built-in. So "register Paperclip as a `deliver:` target" (interim report bullet 3) means **building + running a long-lived Paperclip platform adapter inside the container** (subclass `BasePlatformAdapter`, `connect()`, `send()` POSTing to Paperclip's comment API with its own auth), OR a custom `github_comment`-style branch. ADDING_A_PLATFORM.md is explicit that without a live adapter you get `No live adapter for platform '<name>'` (`:35-39`). That is the WS7 work, not its elimination.

2. **Inbound auth is symmetric-HMAC-per-route, not Paperclip's session/JWT model.** Paperclip→Hermes would require Paperclip to HMAC-sign with a per-container shared secret provisioned onto EFS — a new secret-distribution path Isol8 doesn't have today (its Paperclip↔OpenClaw binding is the seed-agent `adapterConfig.url` WS, not an HMAC webhook). The webhook adapter also has **no notion of which Paperclip company/agent** the call is for beyond the route name + payload text — multi-agent attribution (Friday vs Atlas) must be encoded in route config, compounding crack (b)/multi_agent_registry.

3. **Structured context loss (see §a).** Paperclip's issue→run attribution that OpenClaw's `openclaw_gateway` adapter gives "natively" (per `project_paperclip_openclaw_adapter` memory) degrades to interpolated prompt text. The agent can't reason over `issue.id`/`assignee` as structured fields; it sees a rendered string.

**Honest net:** the webhook adapter is a genuine, overlooked **architectural template** that the spec never evaluated — it proves push-back ingress is idiomatic in Hermes and would dodge the SSE-orphan + poll-fallback machinery the spec resigns itself to. That is a real scope gap worth a loud callout. But it **reduces** WS7 from "bespoke npm transport + SSE orphan handling" to "a Python `BasePlatformAdapter` plugin (inbound HMAC route + outbound Paperclip-comment `send()`) shipped per-version in the container" — lighter and more maintainable, **not** near-zero. The spec's 4-7 eng-week estimate should be re-scoped against the plugin path, but the plugin still has to be written, auth-provisioned, and version-pinned exactly like OpenClaw's externalized plugins.

---

## Return bullets

- **All six spec §4 flags CONFIRM** at file:line (desktop_pairing/device_exec_approval = single shared `API_SERVER_KEY` `api_server.py:647,742-760`; control_ui_proxy = loopback-only ephemeral-token dashboard `web_server.py:86`; resumable_stream = SSE popped on disconnect `api_server.py:3217-3218`; per_agent_workspace = one cwd/identity per process; structured_run_context = flat prompt via `_render_prompt` `webhook.py:625-663`).
- **ADD `durable_runs=❌`** — live runs are in-memory dicts (`api_server.py:658-670`); container restart destroys in-flight Paperclip runs with no resume (only `state.db` snapshots survive). High blast radius given Isol8 restarts containers on every config/image change.
- **ADD `multi_agent_registry=❌`** — `/v1/runs` has no `agent_id` (`api_server.py:2885-2941`); Hermes = one `AIAgent` + ephemeral subagents, no addressable roster. Friday+Atlas has no native home — the single biggest modeling mismatch, omitted from the spec table.
- **The webhook.py push-back ingress is REAL and the spec genuinely missed it** — HMAC inbound (`webhook.py:590-619`) + 202-non-blocking (`:571-584`) + read-not-pop delivery (`:220-264`) + plugin-registrable deliver targets (`config.py:130-159`, `platform_registry.py:205`). It dodges the SSE-orphan/poll-fallback the spec resigns to.
- **CRACK the "shrinks WS7 to near-zero" framing**: there's no `http_post` deliver type; cross-platform delivery needs a **live connected adapter** (`webhook.py:780-785`), so Paperclip-comment-back requires building+running a `BasePlatformAdapter` plugin (or a custom `github_comment`-style branch) — that IS the work, just lighter. Inbound auth is per-route HMAC, not Paperclip's session model (new secret-distribution path), and structured issue context degrades to wake-text.
- **NET / disagreement with interim report**: agree the gap is real and the bespoke-npm-transport framing is wrong; **disagree** that it makes WS7 trivial. Re-scope WS7 as "a version-pinned Python Paperclip platform-adapter plugin in the container" — meaningfully cheaper than the spec's estimate, but still inbound-HMAC + outbound-`send()` + multi-agent-attribution work that the in-memory/no-`agent_id`/no-durable-run limits constrain.
