# Hermes runtime verification — Open question #2: `metadata` propagation

**Date:** 2026-06-16
**Source tree:** `~/Desktop/isol8.nosync/hermes-agent` (Hermes v0.14.0, live on disk)
**Question:** Does a `metadata` field in the `POST /v1/runs` body reach the `AIAgent` and become readable by tools at runtime, so we can pass Paperclip issue/thread context?

## Verdict

**NO. There is no `metadata` field on the `/v1/runs` request schema, and nothing resembling an arbitrary metadata bag flows into the agent.** The run-create handler reads exactly six body keys and silently drops everything else. A custom `metadata` key sent by Isol8 would be ignored.

There *is* a real, structured carrier for per-run context — the `gateway.session_context` contextvars (`HERMES_SESSION_*`) — and tools DO read it. But on the `/v1/runs` path that bag is only ever populated with `platform` and `session_key`; the issue/thread/user fields are left empty. To pass Paperclip issue/thread context we must patch Hermes, not just send a field.

> **Audit drift note:** the prior audit cited `_create_agent ~851-894` and the run lifecycle at `~2862-3157`. `_create_agent` is now at **api_server.py:851-910+** (the signature still ends ~910, not 894) and `_handle_runs` is at **api_server.py:2862-3156**. The lifecycle numbering in the audit's §1.3 has drifted by a few lines but the steps are accurate. All citations below re-grepped against the live file.

---

## 1. The `/v1/runs` request schema reads six keys — `metadata` is not one of them

`_handle_runs` (`gateway/platforms/api_server.py:2862-3156`) parses the JSON body and reads ONLY:

| Body key | Line | Purpose |
|---|---|---|
| `input` | :2885 | the user message (string, or array → last element) |
| `instructions` | :2893 | becomes `ephemeral_system_prompt` |
| `previous_response_id` | :2894 | replay stored history |
| `conversation_history` | :2899 | explicit prior turns |
| `session_id` | :2941 | `body.get("session_id") or stored_session_id or run_id` |
| `model` | :2972 | status/label only |

There is **no `body.get("metadata")`** anywhere in `_handle_runs` (verified by grep: the only `metadata` literal in the whole file is `api_server.py:3510`, which is the `send()` adapter stub's parameter — a documented no-op for the API server, `"API server uses HTTP request/response, not send()"`, **not** part of the inbound run path). Unknown body keys are not validated and not stored — they are dropped.

The stable per-chat scope key comes from the **`X-Hermes-Session-Key` HTTP header**, not the body — parsed via `_parse_session_key_header(request)` at `:2869` into `gateway_session_key`.

## 2. Nothing metadata-shaped flows into `_create_agent` / the `AIAgent`

`_create_agent` (`api_server.py:851-910`) signature accepts only:
`ephemeral_system_prompt`, `session_id`, `stream_delta_callback`, `tool_progress_callback`, `tool_start_callback`, `tool_complete_callback`, `gateway_session_key` (`:851-859`). **No `metadata` parameter.**

The `AIAgent(...)` it constructs (`:893-910`) is passed the same set plus runtime config — again no metadata. The run path calls it with exactly `ephemeral_system_prompt`, `session_id`, the two callbacks, and `gateway_session_key` (`api_server.py:2978-2984`).

`agent/agent_init.py:init_agent` (`:74-208`) accepts `session_id` (:102), `gateway_session_key` (:126), `parent_session_id` (:131), `pass_session_id` (:139) — and lands them as attributes: `agent._gateway_session_key` (`agent_init.py:208`), `agent.session_id` (`:854`/`:859`). **There is no `agent.metadata` / context dict parameter.** (Every other `metadata` token in `agent_init.py` and `run_agent.py` is `model_metadata`, the model-context-length cache, or `_build_memory_write_metadata` — unrelated to request metadata.)

So: a `metadata` field is unreachable by construction. It never reaches `init_agent`, never lands on the `AIAgent` instance, and is not surfaced to `run_conversation` (`api_server.py:3027-3031` passes only `user_message`, `conversation_history`, `task_id`).

## 3. The ONLY runtime-readable per-run context bag is `gateway.session_context` — and `/v1/runs` underfills it

Hermes has a concurrency-safe, tool-readable context bag: `gateway/session_context.py`. It is a **fixed set of contextvars** (`_VAR_MAP`, `session_context.py:66-78`), not arbitrary — only these keys exist:

`HERMES_SESSION_{PLATFORM, CHAT_ID, CHAT_NAME, THREAD_ID, USER_ID, USER_NAME, KEY, ID}` plus three `HERMES_CRON_AUTO_DELIVER_*` keys.

Tools read them via `get_session_env(name, default)` (`session_context.py:133-156`). Confirmed live consumers include `tools/send_message_tool.py:298-300` (reads `HERMES_SESSION_PLATFORM`, `HERMES_SESSION_USER_ID`), `tools/terminal_tool.py`, `tools/cronjob_tools.py`, `tools/approval.py`, `agent/prompt_builder.py`, `agent/skill_utils.py`, and others (grep: 11 files under `tools/` + `agent/`). **So tools CAN read per-run context — but only these named fields, and only if the run path sets them.**

The problem: `_handle_runs`'s `_run_sync` populates this bag with **only two fields** (`api_server.py:3022-3025`):

```py
session_tokens = set_session_vars(
    platform="api_server",
    session_key=approval_session_key,
)
```

`set_session_vars` (`session_context.py:81-107`) supports `chat_id`, `chat_name`, `thread_id`, `user_id`, `user_name` — but the `/v1/runs` caller passes none of them. So on this path `HERMES_SESSION_THREAD_ID`, `_CHAT_ID`, `_USER_ID`, etc. resolve to `""` (the contextvar is set-empty by `clear_session_vars`, and even before that they were never `.set()`). A tool asking "which Paperclip thread am I in?" gets nothing.

## 4. What this means for passing Paperclip issue/thread context (forward-looking)

This is a small, localized patch — NOT a redesign:

- **Add `metadata` (or named fields) to `_handle_runs`** body parsing (~`api_server.py:2885-2941`), e.g. `body.get("metadata", {})`.
- **Forward it into `set_session_vars`** at `api_server.py:3022-3025` by mapping Paperclip `issue_id`/`thread_id` onto the existing `thread_id`/`chat_id`/`user_id` slots, OR
- **Extend `gateway/session_context.py`** with a new `HERMES_SESSION_ISSUE_ID` (or a generic `HERMES_SESSION_METADATA` JSON blob) contextvar + `_VAR_MAP` entry + `set_session_vars` kwarg, then read it from whatever tool needs it.

The existing `HERMES_SESSION_THREAD_ID` slot is the most natural fit for a Paperclip thread id with zero new plumbing on the tool side — it is already read across the tool layer. This is a fork patch on the `isol8/*` branch model, comparable in size to the existing Hermes patches.

---

## Citations (re-grepped, live)

- `gateway/platforms/api_server.py:2862-3156` — `_handle_runs` body parse; six keys; no `metadata`.
- `gateway/platforms/api_server.py:2869` — session key from `X-Hermes-Session-Key` header, not body.
- `gateway/platforms/api_server.py:2978-2984` — `_create_agent(...)` call args (no metadata).
- `gateway/platforms/api_server.py:3022-3025` — `set_session_vars(platform="api_server", session_key=...)` — only two fields filled.
- `gateway/platforms/api_server.py:3027-3031` — `run_conversation(user_message, conversation_history, task_id)`.
- `gateway/platforms/api_server.py:851-910` — `_create_agent` signature + `AIAgent(...)`; no metadata param.
- `gateway/platforms/api_server.py:3505-3515` — `send(..., metadata=None)` is the no-op outbound adapter stub (the file's only `metadata` literal); not the run path.
- `agent/agent_init.py:74-208` — `init_agent` signature; `agent._gateway_session_key = gateway_session_key` at :208; no metadata param.
- `agent/agent_init.py:852-859` — `agent.session_id` assignment.
- `run_agent.py:349-470` — `AIAgent.__init__` params; no metadata.
- `gateway/session_context.py:66-78` — fixed `_VAR_MAP`; the complete set of runtime-readable context keys.
- `gateway/session_context.py:81-107` — `set_session_vars` kwargs (platform, chat_id, chat_name, thread_id, user_id, user_name, session_key).
- `gateway/session_context.py:133-156` — `get_session_env` (what tools call).
- `tools/send_message_tool.py:298-300` — example tool reading `HERMES_SESSION_*`.
