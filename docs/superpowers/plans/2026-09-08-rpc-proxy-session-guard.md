# RPC Proxy Session Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the cross-tenant RPC surface on the shared per-owner container by making the generic WebSocket RPC proxy default-deny, and by widening the session-key ownership guard from 8 hand-maintained methods to the full vendored upstream registry.

**Architecture:** Two independent changes to `apps/backend/routers/websocket_chat.py`, shipped as two PRs. PR A adds `_PROXY_ALLOWED_METHODS`, a default-deny frozenset checked in `_process_rpc_background` before the container is ever resolved — it turns a ~390-method forwarding surface into a 44-method one. PR B vendors OpenClaw's own `SESSION_TARGET_FIELDS_BY_METHOD` table into Python, unions it with our existing entries, teaches the guard to iterate *every* key field a method carries (not one), and adds the three key-bearing methods upstream's table structurally omits. PR A is the fence; PR B is what makes the next allowlist addition safe by default.

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio (`asyncio_mode = "auto"`), ruff (line-length 120). No new dependencies.

**Spec:** This document. It supersedes the original audit finding, three parts of which are **wrong** — see "Corrections to the finding" below. Read that section before writing any code.

---

## Delivery model — READ THIS FIRST

**This change is BACKEND-ONLY.** It touches `apps/backend/routers/websocket_chat.py`,
`apps/backend/core/services/`, `apps/backend/scripts/` and `apps/backend/tests/`. Nothing
in it touches `openclaw.json`, EFS, the extended image, or any per-user container.

Therefore:

- **No container roll. No config patch. No image bump.** Do not schedule one; there is
  nothing for a container to pick up.
- Delivery to all users is **one prod promotion** — `gh workflow run deploy.yml --ref main`,
  or Actions → deploy → Run workflow. Merging to `main` deploys **dev only**; every prod job
  in `deploy.yml` carries `if: github.event_name == 'workflow_dispatch'`.
- Until that dispatch runs, prod is still forwarding everything. Do not tell anyone the fix
  is live because it merged.
- Validate on **dev** first, via Claude-in-Chrome against `dev.flickmeon.ai` (repo rule:
  E2E is Claude-in-Chrome on dev, never the journey test, never prod), then promote once at
  the end.

---

## Corrections to the finding

The audit finding this plan came from is right about the shape of the problem and wrong
about three specifics. Two of them would have had you build a guard for something that is
already guarded. All three were verified by reading code at `origin/main` and at the pinned
fork tag.

### ❌ WRONG — "`chat.send` is unguarded through the generic proxy"

This was the finding's headline "critical case". It is false, and has been false since
2026-07.

`apps/backend/routers/websocket_chat.py:306`:

```python
_RUN_DISPATCH_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})
```

`apps/backend/routers/websocket_chat.py:1923-1948` refuses every one of them at the very top
of `_process_rpc_background`, **before** `resolve_running_container` is called, with a
structured `INVALID_REQUEST` pointing the caller at the typed `agent_chat` / `agent_steer`
frame. Verified present at `origin/main` (`git show origin/main:apps/backend/routers/websocket_chat.py`,
line 306) and pinned by `apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py:167`.

**A member cannot write into another member's conversation through this proxy today.** Do
not plan, build, or claim a fix for that. If you are writing a PR description, do not repeat
the claim.

### ❌ WRONG — three of the named methods

The finding listed `sessions.preview`, `sessions.patchMany` and `sessions.visibility.set`
among the "~12 more session-key-bearing methods the registry covers". Checked against
`v2026.8.1-isol8.3`:

| finding says | reality |
|---|---|
| `sessions.preview` is in the registry | **It is not.** It exists (`src/gateway/server-methods/sessions-read.ts:540`) and takes a **list** param `keys` (`:544`), but it is absent from `SESSION_TARGET_FIELDS_BY_METHOD`. Porting the registry does **not** guard it. |
| `sessions.patchMany` is in the registry | **It is not in the map.** Upstream handles it *outside* the map, in `resolveSessionMutationTargets` (`session-sharing-target-input.ts:271-286`), reading `targets[].key`. A verbatim map port leaves the `sessions.patch` bypass open. |
| `sessions.visibility.set` exists | **No such method.** `git grep '"sessions.visibility.set"' v2026.8.1-isol8.3` returns nothing. The real name is `session.visibility.set` (singular `session`), and it too is absent from the registry. |

### ⚠️ OVERSTATED — "Upstream maintains the map for us, exhaustively"

It is exhaustive for the job upstream built it for — authorizing session-*sharing*
*mutations*. It is not an inventory of methods that carry a caller-supplied session key.

Scanning every non-test handler in `src/gateway/server-methods/` at `v2026.8.1-isol8.3` for
reads of `params.key` / `params.sessionKey` / `params.keys` / `params.sessionKeys` and
subtracting the 52 registry rows leaves these **absent from the registry**:

```
audit.activity.list      session.suggestions.add     sessions.files.get
audit.list               session.suggestions.list    sessions.files.list
chat.toolTitles          session.suggestions.resolve sessions.files.reveal
session.discussion.info  session.typing              sessions.preview
session.members.add      session.visibility.set      sessions.viewers.set
session.members.remove   sessions.describe           tasks.list
                         sessions.diff               taskSuggestions.list
```

(Plus `push.web.subscribe` and `users.prefs.get`, whose `keys` params are webpush crypto
keys and preference names — not session keys. And `system.info`, whose `sessionKey` is
optional context.)

That is ~19 real cross-tenant reads and writes the registry does not model. **This is the
decisive argument for the ordering below:** the registry port closes 43 doors; the
default-deny allowlist closes all ~390. The registry cannot be the fence.

### ✅ CONFIRMED

Everything else in the finding checks out:

- No method allowlist on the proxy. `_process_rpc_background` forwards `method` verbatim to
  `pool.send_rpc` (`websocket_chat.py:2103`).
- The tenant fence is an opt-in 8-entry map (`websocket_chat.py:1396-1411`) plus a 1-entry
  list map (`:1413-1417`).
- `owner_id = org_id or user_id`; org members share ONE container, so that map is the entire
  boundary between member A and member B.
- `roleVisibilityFilter` only engages behind `hasOperatorBoundary`
  (`src/gateway/operator-role-policy.ts:138-140`), which is `operatorSessionCap(...) !== undefined`.
  We configure no operator role, so it is `undefined` and the filter never runs — visible at
  the `sessions.preview` call site, `sessions-read.ts:557-559`.
- Our device holds `operator.admin` (`apps/backend/core/crypto/operator_device.py:85-89`,
  `BACKEND_OPERATOR_SCOPES`), so the container trusts us for everything.
- `SESSION_TARGET_FIELDS_BY_METHOD` is real, at `src/gateway/session-sharing-target-input.ts:19-75`,
  with `REQUIRED_SESSION_TARGET_METHODS` (`:77-122`), `APPROVAL_SESSION_TARGET_METHODS`
  (`:124-128`), `READ_ONLY_SESSION_TARGET_METHODS` (`:130-133`) and
  `sessionMutationTargetFields()` (`:146-150`).
- `sessions.create` carries two fields, `["key", "parentSessionKey"]` — our guard reads one.

### One trap in the upstream accessor

**Do not port `sessionMutationTargetFields()`.** It returns `[]` for
`READ_ONLY_SESSION_TARGET_METHODS` — `sessions.companion.ask` and `sessions.companion.state`
(`session-sharing-target-input.ts:130-133, 146-150`). Upstream is asking "is this a mutation
I must authorize?"; we are asking "is this a cross-tenant access?". `sessions.companion.ask`
hands another member's transcript to a model and returns the summary. **Port the raw map.**

---

## Global Constraints

- **Fork reads use the pinned tag, never the working tree.** `~/dev/agent-fork` is checked
  out at a stale 2026.7.2. Read with
  `git -C ~/dev/agent-fork show v2026.8.1-isol8.3:<path>` and
  `git -C ~/dev/agent-fork grep -n '<pattern>' v2026.8.1-isol8.3`. **Never** run
  `git checkout` / `git switch` / `git pull` in that repo.
- **We run `v2026.8.1-isol8.3`.** `openclaw-version.json#upstream` is
  `ghcr.io/thelightbulbcompany/agent:2026.8.1-isol8.3`; the git tag is that with a `v`
  prefix on the version.
- **A base bump MUST re-vendor in the same PR.** Infra-only PRs skip the backend suite, so
  a bump that leaves the vendored registry stale ships silently. This is enforced by
  `test_session_target_registry_tag_matches_the_pinned_fork_base` (Task 3) — an
  always-running test, not a chaos-marked one.
- **ruff line-length is 120** (`apps/backend/pyproject.toml:48`).
- **Backend tests live under `apps/backend/tests/`** and CI runs `uv run pytest tests/ -v`
  (`.github/workflows/backend.yml:79`), which includes `tests/chaos/`.
- **Compose with PR #2348, do not re-plan it.** `_FORBIDDEN_PARAMS_BY_METHOD`
  (`websocket_chat.py:1437-1439`) and its guard branch (`:1476-1496`) already ship the
  params-level `permissionMode` check. Every change here runs alongside it; the ordering
  inside `_guard_session_key_ownership` (forbidden-params check first, then ownership) is
  preserved.
- **Do not add anything to `_RUN_DISPATCH_METHODS`.** `test_run_dispatch_refusal_set_is_small_and_principled`
  (`test_websocket_chat_run_dispatch_guard.py:165-167`) asserts it stays exactly those four,
  deliberately, "to guard against the set silently growing into a maintained allowlist". The
  allowlist is a separate mechanism with a separate table.

---

## Ordering and PR split — the argument

The finding proposed registry-first ("pure widening of an existing guard"), allowlist-second
("behaviour-changing"). **Reverse it.** Ship the allowlist first.

**Why the finding's order is wrong.** The registry port's security value collapses the moment
default-deny lands: of the 47 methods the port newly guards, 4 are already refused by
`_RUN_DISPATCH_METHODS` and **43 of the remaining 43 are denied outright by the allowlist**.
Not one of them is on the frontend inventory. So registry-first buys three weeks of partial
coverage over ~43 of ~390 doors, while `sessions.preview`, `session.members.add`,
`session.visibility.set`, `sessions.diff` and ~15 more stay wide open — because, as shown
above, the registry does not model them.

**Why "pure widening" is not quite pure either.** Exactly one method in the registry delta is
a live frontend call site: `progressCard.get` (`apps/frontend/src/hooks/useProgressCard.ts:40,54`).
Widening the guard starts ownership-checking it. It is safe — the key it sends is
`buildChatSessionKey(agentId, userId, activeSessionId)` from
`apps/frontend/src/components/chat/AgentChatWindow.tsx:277-279`, where
`sessionName = userId` (`:242`), producing an owned `agent:<id>:<userId>[:<uuid>]` key that
`_user_owns_session_key` accepts. But that took a verification pass, so "pure widening, zero
risk" was an assumption, not a fact.

**Why allowlist-first is safe enough.** Its only risk is inventory error, and that risk is
bounded three ways: (1) a positive regression test asserting all 44 inventoried methods still
forward (Task 2), which is the same technique the repo already uses at
`test_websocket_chat_run_dispatch_guard.py:187-197`; (2) the refusal emits
`put_metric("rpc.refused", dimensions={"method": …, "reason": "not_allowlisted"})`, so a
missed method surfaces in CloudWatch **by name** within minutes of the dev deploy, before the
prod promotion; (3) dev validation via Claude-in-Chrome before promoting.

**Deliberately no feature flag.** A security control behind a flag is a control that is off,
and it costs a second prod promotion to turn on. The metric is the safety valve.

### The split

| PR | Contents | Risk | Tasks |
|---|---|---|---|
| **A — default-deny allowlist** | `_PROXY_ALLOWED_METHODS` + refusal + metric; positive inventory test; reconcile the stale list in the existing run-dispatch test | Behaviour-changing. Bounded by the inventory + metric. | 1, 2 |
| **B — vendored registry** | Vendored `SESSION_TARGET_FIELDS_BY_METHOD`; regen script; tag-pin guard; multi-field union guard; `patchMany` + the three list-param methods; fork-diff drift test | Pure widening. Every newly-guarded method is already denied by PR A. | 3, 4, 5, 6 |

Land A, deploy to dev, walk the product in Chrome, promote. Then land B on top.

---

## The method inventory (PR A's riskiest input — show your work)

A default-deny list that omits a live method is an outage. This inventory was built four
independent ways and reconciled. **Two of the four missed something the others caught**,
which is why all four are recorded here.

**The choke point.** Exactly one place in client TS builds the frame:
`packages/shared/src/gateway/useGatewayConnection.ts:470` —
`JSON.stringify({ type: "req", id, method, params: params || {} })`. Everything reaches it
through `sendReq`, `useGatewayRpc` (`packages/shared/src/hooks/useGatewayRpc.ts:127`) or
`useGatewayRpcMutation` (`:217`).

**Method 1 — literal first argument, same line.**
`grep -rnoE '(useGatewayRpc|useGatewayRpcMutation|callRpc|sendReq|rpc)(<[^>]*>)?\(\s*"[a-zA-Z][a-zA-Z0-9._]*"' apps/frontend/src packages/shared/src apps/mobile/src`
→ 23 methods. Misses every multi-line call and every const-defined method name.

**Method 2 — intersect all quoted dotted strings in client code with the upstream method
universe.**
`grep -rhoE '"[a-z][a-zA-Z0-9]*(\.[a-zA-Z0-9]+)+"' --include='*.ts' --include='*.tsx' apps/frontend/src packages/shared/src apps/mobile/src`
(156 unique strings) intersected with the 390 dotted method names in
`src/gateway/methods/core-descriptors.ts` at the pinned tag → 39 methods. Catches multi-line
and const cases. Misses `health` (no dot) and `browser.request` (a **plugin**-provided
method, absent from `core-descriptors.ts`).

**Method 3 — per-namespace sweep** over `cron.*`, `talk.*`, `board.*`, `mcp.*`, `audit.*`,
`device.*`, `artifacts.*`, `tasks.*`, `session.*`, `plugins.*`, `push.*`, `users.*` and 10
more, intersected with the same universe → returned only the six `sessions.*` methods
already known. **Confirms no `cron`, `talk`, `board`, `mcp`, `audit` or `device` method is
sent from any client.**

**Method 4 — a delegated exhaustive read** of `apps/frontend/`, `packages/shared/`,
`apps/mobile/` and `apps/desktop/` → 40 methods, including the const-defined and
ternary-computed sites. It **missed `agent.identity.get`**
(`apps/frontend/src/components/control/panels/AgentOverviewTab.tsx:35`), which methods 2 and
3 caught.

**Reconciliation against the stale in-repo list.** `FRONTEND_RPC_METHODS` in
`apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py:38-72` is a
2026-07-12 grep of the same thing, 35 entries. Versus today's union it is missing
`browser.request`, `progressCard.get`, `question.list`, `question.resolve`,
`sessions.branches.list`, `sessions.branches.switch`, `sessions.rewind` — seven methods added
in the two months since. Task 2 replaces it.

### Dynamic / computed method names

Four sites do not pass a literal. All four are closed:

1. `apps/frontend/src/components/control/panels/SkillProposalsPanel.tsx:93` —
   ``callRpc(`skills.proposals.${action}`, …)``, where `action` is typed
   `"apply" | "reject"` at `:88`. Closure: `skills.proposals.apply`, `skills.proposals.reject`.
   **Invisible to every literal grep.**
2. `apps/frontend/src/components/control/panels/DreamsPanel.tsx:283` — `callRpc(method, params)`
   where `method` comes from a module-level table at `:259-265`. Enumerated as the six
   `doctor.memory.*` entries. **Highest future drift risk:** adding a row to that table ships
   a new method with no change at the call site.
3. `apps/frontend/src/hooks/useExecApprovals.tsx:547` — `sendReq(method, …)`, `method` from
   the ternary at `:542-545`. Closure: `plugin.approval.resolve`, `exec.approval.resolve`.
4. `packages/shared/src/hooks/useGatewayRpc.ts:119-127` — the SWR fetcher re-derives the
   method by slicing the cache key. Not a new source (keys are minted by `gatewayRpcKey` at
   `:83`), but it means static analysis of `sendReq` arguments will never see a literal for
   any read method. **A lint- or codemod-based enforcement would silently pass; a runtime
   allowlist is the right shape.**

Two sites prop-drill `callRpc` with the open type `(method: string, params?) => Promise<T>` —
`SkillsTab.tsx:487` and `awaitChannelReady.ts:32`. Their receivers pass literals today, but
a future child can send anything without touching a file that greps as an RPC site. The
metric in Task 1 is what catches that.

### Two grep traps

- `apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx:58` reads
  `callRpc("browser.request", { method: "POST", path: "/start" })`. The inner `method: "POST"`
  is an HTTP verb inside `params`. Any `grep 'method:'` audit trips on it.
- `apps/frontend/src/components/teams/panels/RunDetailPanel.tsx:148` passes
  `` sessionKey: `run:${runId}` `` — a **local rendering value** handed to `buildChatItems`,
  never an RPC param. It looks exactly like a session key that `_user_owns_session_key` would
  reject. It is not sent anywhere.

### Not on the list, and why

- `chat.abort`, `agent.wait`, `tools.invoke`, `cron.list` — **no client caller.** They appear
  only in `test_websocket_chat_run_dispatch_guard.py:175` as "run-adjacent near-misses that
  still forward". Aborts reach the container through the typed `agent_abort` frame
  (`websocket_chat.py:1248`), not this proxy. Task 2 updates that test.
- `config.set` — JSDoc example only (`packages/shared/src/hooks/useGatewayRpc.ts:36`), zero
  real callers. **Kept anyway**: it is in the current in-repo list, it is a plain config
  write, and dropping it buys nothing.
- `sessions.search` and `chat.message.get` — no live client caller either, but both already
  carry an individual ownership guard (`_SESSION_KEYS_LIST_PARAM_BY_METHOD` /
  `_SESSION_KEY_PARAM_BY_METHOD`), and `sessions.search` additionally has a response
  post-filter (`_filter_search_hits_for_user`). **Kept**, under the stated rule below.

**The rule for what goes on the allowlist:** *a method the product demonstrably sends, or a
method already individually ownership-guarded.* Guard-map membership alone is not evidence of
use, but it is evidence the call is safe to admit.

### Out of scope — the two other doors on this socket

Neither reaches `_process_rpc_background`, and neither may be allowlisted:

- **Desktop node host** (`clientId=openclaw-macos`, in `NODE_HOST_CLIENT_IDS` at
  `websocket_chat.py:409`). Sends `connect` and `node.invoke.result` only
  (`apps/desktop/src-tauri/src/node_client.rs:228,703`). Diverted at `websocket_chat.py:1095-1131`
  (node connect) and `:1145-1150` (`handle_node_message`), both of which `return` **before**
  the dispatch at `:1152`.
- **Paperclip `openclaw_gateway` adapter** (`clientId=paperclip-operator`). A verbatim relay
  (`core/services/operator_proxy.py:348-355`) running its own device-auth handshake with its
  own key; diverted at `websocket_chat.py:969`. It sends `agent` / `agent.wait` to drive runs
  — allowlisting it the same way would break every Teams run.

**Placement is therefore load-bearing.** The check goes *inside* `_process_rpc_background`.
Put it any earlier — at frame parse, or anywhere above `websocket_chat.py:969` — and you
break the desktop app and every Paperclip run.

---

## File Structure

**PR A**

- Modify: `apps/backend/routers/websocket_chat.py` — add `_PROXY_ALLOWED_METHODS` near
  `_RUN_DISPATCH_METHODS` (~`:306-330`); add the refusal in `_process_rpc_background`
  immediately after the run-dispatch block (~`:1948`).
- Create: `apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py` — denial,
  metric, and the full positive inventory.
- Modify: `apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py` — its
  `FRONTEND_RPC_METHODS` list is stale and four of its "sibling" methods are now denied.

**PR B**

- Create: `apps/backend/core/services/openclaw_session_targets.py` — the vendored registry,
  a plain Python dict + `SOURCE_TAG`. Lives next to its consumer, mirroring
  `core/services/openclaw_config.schema.json` next to `core/services/config_schema.py`.
- Create: `apps/backend/scripts/vendor_session_target_registry.py` — regenerates that module
  from the pinned fork tag; `--check` mode is imported by the drift test (re-runnable ops
  tools live in `scripts/`, per CLAUDE.md).
- Create: `apps/backend/tests/unit/services/test_openclaw_session_targets.py` — the
  always-running tag-pin guard.
- Create: `apps/backend/tests/chaos/test_session_target_registry_drift.py` — the
  fork-diff guard; skips when the fork checkout is absent (as CI's is).
- Modify: `apps/backend/routers/websocket_chat.py` — replace `_SESSION_KEY_PARAM_BY_METHOD`
  with the multi-field union `_SESSION_KEY_PARAMS_BY_METHOD`; extend
  `_SESSION_KEYS_LIST_PARAM_BY_METHOD`; add `sessions.patchMany`.
- Modify: `apps/backend/tests/unit/routers/test_websocket_session_key.py` — extend for the
  new coverage.

---

## Task 1: Default-deny allowlist on the RPC proxy

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:306-330` (table), `:1948` (refusal)
- Test: `apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py` (create)

**Interfaces:**
- Consumes: `_RUN_DISPATCH_METHODS` (`:306`), `put_metric` (imported `:27`),
  `management_api` / `get_management_api_client`.
- Produces: `websocket_chat._PROXY_ALLOWED_METHODS: frozenset[str]` — Task 2 and any future
  UI method addition read this name.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py`:

```python
"""The generic RPC proxy forwards only what the product actually sends.

`_process_rpc_background` used to forward ANY `method` string a browser put in a
`{"type":"req"}` frame straight to the per-owner container. The container trusts
us completely: our operator device holds `operator.admin`
(core/crypto/operator_device.py:85-89), and OpenClaw's own `roleVisibilityFilter`
only engages behind `hasOperatorBoundary` (fork src/gateway/operator-role-policy.ts:138-140),
which needs a configured operator role — we configure none.

Orgs share ONE container (`owner_id = org_id or user_id`), so an unguarded method
that carries a session key is a cross-tenant IDOR, and one that carries none at all
(audit reads, device pairing, cron writes) cannot be fixed by any key check.
`core-descriptors.ts` at the pinned tag declares ~390 dotted methods; the product
sends 44 of them. Deny the rest.

This is default-deny, so the table IS the security boundary: see the inventory in
docs/superpowers/plans/2026-09-08-rpc-proxy-session-guard.md for how it was built and
what may be added to it.
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from routers import websocket_chat as wc  # noqa: E402


def _harness(monkeypatch):
    """Wire _process_rpc_background's collaborators; return (sent, pool, ecs)."""
    sent: list[dict] = []

    mgmt = MagicMock()
    mgmt.send_message = MagicMock(side_effect=lambda _cid, payload: sent.append(payload))
    mgmt.send_message_async = AsyncMock(side_effect=mgmt.send_message)
    monkeypatch.setattr(wc, "management_api", None)
    monkeypatch.setattr(wc, "get_management_api_client", AsyncMock(return_value=mgmt))

    pool = MagicMock()
    pool.send_rpc = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(wc, "get_gateway_pool", MagicMock(return_value=pool))

    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "gw"}, "10.0.0.5"))
    monkeypatch.setattr(wc, "get_ecs_manager", MagicMock(return_value=ecs))
    return sent, pool, ecs


async def _run(method: str, params: dict | None = None):
    await wc._process_rpc_background(
        connection_id="conn-abcdefghijkl",
        user_id="user_1",
        owner_id="user_1",
        req_id="req-1",
        method=method,
        params=params if params is not None else {},
    )


# Methods that are real, reachable, dangerous, and that nothing of ours sends.
_DENIED = [
    "sessions.preview",            # reads another member's transcript
    "sessions.companion.ask",      # model-summarises another member's transcript
    "sessions.patchMany",          # routes around the sessions.patch guard
    "sessions.fork",
    "sessions.assignOwner",
    "session.members.add",
    "session.visibility.set",
    "sessions.diff",
    "sessions.files.get",
    "cron.add",                    # carries no session key at all
    "device.pair.approve",
    "audit.list",
]


@pytest.mark.parametrize("method", _DENIED)
@pytest.mark.asyncio
async def test_unlisted_method_is_refused_before_touching_the_container(monkeypatch, method):
    sent, pool, ecs = _harness(monkeypatch)

    await _run(method, {"sessionKey": "agent:a:user_2"})

    pool.send_rpc.assert_not_awaited()
    ecs.resolve_running_container.assert_not_awaited()
    assert len(sent) == 1
    frame = sent[0]
    assert frame["type"] == "res"
    assert frame["id"] == "req-1"
    assert frame["ok"] is False
    assert frame["error"]["code"] == "INVALID_REQUEST"
    assert frame["error"]["details"]["method"] == method


@pytest.mark.asyncio
async def test_refusal_emits_a_named_metric(monkeypatch):
    """The dev-deploy safety valve: a method we wrongly omitted shows up in
    CloudWatch BY NAME before the prod promotion."""
    _harness(monkeypatch)
    calls: list[tuple] = []
    monkeypatch.setattr(wc, "put_metric", lambda name, **kw: calls.append((name, kw)))

    await _run("cron.add", {})

    assert ("rpc.refused", {"dimensions": {"method": "cron.add", "reason": "not_allowlisted"}}) in calls


@pytest.mark.asyncio
async def test_run_dispatch_methods_keep_their_own_more_helpful_refusal(monkeypatch):
    """chat.send is denied by BOTH gates. The run-dispatch one runs first because
    it names the typed frame the caller should use instead."""
    sent, pool, _ = _harness(monkeypatch)

    await _run("chat.send", {"agentId": "luna", "message": "hi"})

    pool.send_rpc.assert_not_awaited()
    assert sent[0]["error"]["details"]["use"] == "agent_chat"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_method_allowlist.py -v
```

Expected: FAIL — `AttributeError` on `wc._PROXY_ALLOWED_METHODS` is not raised yet; instead
the denial tests fail with `pool.send_rpc.assert_not_awaited()` because every method still
forwards.

- [ ] **Step 3: Add the allowlist table**

In `apps/backend/routers/websocket_chat.py`, immediately after the `_WAKE_GATED_METHODS`
assignment (currently `:327`), insert:

```python
# DEFAULT-DENY: the only gateway methods the browser/mobile RPC proxy will forward.
#
# `core-descriptors.ts` at the pinned fork tag declares ~390 dotted methods, plus
# plugin-provided ones. The product sends 44. Everything else was reachable from any
# org member's browser against the SHARED per-owner container — and the container has
# no opinion about it: our operator device holds `operator.admin`
# (core/crypto/operator_device.py), and OpenClaw's `roleVisibilityFilter` only engages
# behind `hasOperatorBoundary` (fork src/gateway/operator-role-policy.ts), which needs a
# configured operator role. We configure none.
#
# A session-key ownership check cannot be the fence here: roughly half the dangerous
# surface (cron writes, device pairing, audit reads, config apply) carries no session
# key at all. This table is the fence; _SESSION_KEY_PARAMS_BY_METHOD is depth behind it.
#
# WHAT MAY BE ADDED: a method the product demonstrably sends, or one already carrying an
# individual ownership guard. Nothing else. Adding a line here re-opens a door, so the
# justification belongs in the PR, not in a comment.
#
# HOW TO SPOT AN OMISSION: a refusal emits put_metric("rpc.refused", reason="not_allowlisted")
# with the method name. Watch it on dev after the merge, before the prod promotion.
#
# Deliberately NOT here (no client caller; the near-misses that look like they belong):
#   chat.abort / agent.wait / tools.invoke / cron.list — aborts ride the typed
#   `agent_abort` frame, not this proxy.
# Deliberately NOT here (different door, diverted upstream of this function):
#   connect, node.invoke.result (desktop node host, websocket_chat.py ~:1145);
#   agent, agent.wait (Paperclip operator relay, ~:969).
_PROXY_ALLOWED_METHODS = frozenset(
    {
        # agents + identity
        "agent.identity.get",
        "agents.create",
        "agents.delete",
        "agents.list",
        "agents.update",
        # browser live-view panel (plugin-provided; absent from core-descriptors.ts)
        "browser.request",
        # channels
        "channels.status",
        # chat reads (key-guarded)
        "chat.history",
        "chat.message.get",
        "commands.list",
        # config
        "config.get",
        "config.patch",
        "config.set",
        # dreams / memory doctor (DreamsPanel.tsx dispatches these from a table)
        "doctor.memory.backfillDreamDiary",
        "doctor.memory.dedupeDreamDiary",
        "doctor.memory.dreamDiary",
        "doctor.memory.repairDreamingArtifacts",
        "doctor.memory.resetDreamDiary",
        "doctor.memory.resetGroundedShortTerm",
        "doctor.memory.status",
        # approvals
        "exec.approval.resolve",
        "plugin.approval.resolve",
        # health / models
        "health",
        "models.authStatus",
        "models.list",
        # progress card
        "progressCard.get",
        # questions
        "question.list",
        "question.resolve",
        # sessions (all key-guarded or post-filtered)
        "sessions.branches.list",
        "sessions.branches.switch",
        "sessions.delete",
        "sessions.list",
        "sessions.patch",
        "sessions.rewind",
        "sessions.search",
        # skills
        "skills.install",
        "skills.proposals.apply",  # `skills.proposals.${action}` template literal
        "skills.proposals.inspect",
        "skills.proposals.list",
        "skills.proposals.reject",  # same template literal
        "skills.search",
        "skills.status",
        "skills.update",
        # tools
        "tools.catalog",
    }
)
```

- [ ] **Step 4: Add the refusal**

In `_process_rpc_background`, immediately after the `_RUN_DISPATCH_METHODS` block's closing
`return` (currently `:1948`) and **before** `try:` / `get_ecs_manager()`, insert:

```python
    # Default-deny. See _PROXY_ALLOWED_METHODS. Refuse before touching ECS or the pool
    # so a denied call is a no-op on the container. This runs AFTER the run-dispatch
    # check above so chat.send keeps its more useful "use agent_chat" message.
    if method not in _PROXY_ALLOWED_METHODS:
        logger.warning(
            "[%s] rpc %s refused (not on the proxy allowlist) owner=%s conn=%s",
            user_id,
            method,
            owner_id,
            connection_id[:12],
        )
        put_metric("rpc.refused", dimensions={"method": method, "reason": "not_allowlisted"})
        await management_api.send_message_async(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": f'"{method}" is not available over the RPC proxy.',
                    "details": {"method": method},
                },
            },
        )
        return
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_method_allowlist.py -v
```

Expected: PASS, 15 tests.

- [ ] **Step 6: Lint**

```bash
cd apps/backend && uv run ruff check routers/websocket_chat.py tests/unit/routers/test_websocket_chat_method_allowlist.py && uv run ruff format --check routers/websocket_chat.py tests/unit/routers/test_websocket_chat_method_allowlist.py
```

Expected: no findings. If `ruff format` rewrites anything, re-add the files before committing.

- [ ] **Step 7: Commit**

```bash
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py
git commit -m "fix(ws): default-deny the RPC proxy instead of forwarding ~390 methods"
```

---

## Task 2: Pin the inventory and reconcile the stale run-dispatch test

Task 1's table is only as good as the evidence that every listed method still works. This
task adds the positive half and repairs the two-month-old list in the neighbouring test,
whose four "sibling" methods the allowlist now denies.

**Files:**
- Modify: `apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py`
- Modify: `apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py:38-72` (list), `:175-184` (siblings)

**Interfaces:**
- Consumes: `websocket_chat._PROXY_ALLOWED_METHODS` from Task 1.
- Produces: nothing new; tests only.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py`:

```python
# --------------------------------------------------------------------------
# The positive half: every allowlisted method must still reach the container.
#
# Session-key-bearing methods need a key the caller owns, or
# _guard_session_key_ownership consumes the request before it forwards.
# --------------------------------------------------------------------------

_OWNED_KEY = "agent:luna:user_1"

_PARAMS_BY_METHOD: dict[str, dict] = {
    "agents.delete": {"agentId": "agent_bob"},
    "chat.history": {"sessionKey": _OWNED_KEY},
    "chat.message.get": {"sessionKey": _OWNED_KEY},
    "progressCard.get": {"sessionKey": _OWNED_KEY},
    "sessions.branches.list": {"sessionKey": _OWNED_KEY},
    "sessions.branches.switch": {"sessionKey": _OWNED_KEY},
    "sessions.delete": {"key": _OWNED_KEY},
    "sessions.patch": {"key": _OWNED_KEY},
    "sessions.rewind": {"sessionKey": _OWNED_KEY},
    "sessions.search": {"sessionKeys": [_OWNED_KEY]},
}


def _forwarding_harness(monkeypatch):
    sent, pool, ecs = _harness(monkeypatch)
    # Interceptors / guards that would otherwise reach Paperclip, EFS or DDB.
    monkeypatch.setattr(wc, "_intercept_agents_create", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "_intercept_agents_update", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "is_seeded_agent", AsyncMock(return_value=False))
    monkeypatch.setattr(wc, "prepare_paperclip_agent_delete", AsyncMock(return_value={}))
    monkeypatch.setattr(wc, "delete_paperclip_for_chat", AsyncMock(return_value=None))
    import core.containers as containers

    monkeypatch.setattr(
        containers,
        "get_workspace",
        MagicMock(return_value=MagicMock(cleanup_agent_dirs=MagicMock())),
    )
    return sent, pool, ecs


@pytest.mark.parametrize("method", sorted(wc._PROXY_ALLOWED_METHODS))
@pytest.mark.asyncio
async def test_every_allowlisted_method_still_forwards(monkeypatch, method):
    """Drives the list itself, so it can never claim coverage it does not have."""
    sent, pool, _ = _forwarding_harness(monkeypatch)

    await _run(method, dict(_PARAMS_BY_METHOD.get(method, {})))

    pool.send_rpc.assert_awaited_once()
    assert pool.send_rpc.await_args.kwargs["method"] == method
    assert sent[-1]["type"] == "res"
    assert sent[-1]["ok"] is True


@pytest.mark.asyncio
async def test_the_allowlist_and_the_run_dispatch_set_are_disjoint():
    """chat.send must never be admitted by adding it here — the typed agent_chat
    frame owns that job, and _RUN_DISPATCH_METHODS is deliberately frozen at four."""
    assert not (wc._PROXY_ALLOWED_METHODS & wc._RUN_DISPATCH_METHODS)


@pytest.mark.asyncio
async def test_the_template_literal_methods_are_both_present():
    """SkillProposalsPanel.tsx:93 sends `skills.proposals.${action}` with action typed
    "apply" | "reject". Neither is visible to a literal grep, so pin them."""
    assert {"skills.proposals.apply", "skills.proposals.reject"} <= wc._PROXY_ALLOWED_METHODS
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_method_allowlist.py -v -k "forwards or disjoint or template"
```

Expected: mostly PASS, with FAIL on any allowlisted method whose params fixture is missing —
it surfaces as `assert sent[-1]["ok"] is True` failing, because `_guard_session_key_ownership`
consumed the request instead of forwarding it. **The fix is the fixture, never the allowlist:**
a key-bearing method needs an owned key in `_PARAMS_BY_METHOD`, not removal from the list.

- [ ] **Step 3: Make it pass**

No production code changes. If a method fails, the cause is a missing entry in
`_PARAMS_BY_METHOD` — add the owned key it needs. Re-run until green.

- [ ] **Step 4: Repair the stale neighbouring test**

In `apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py`, replace the
whole `FRONTEND_RPC_METHODS` block (`:28-72`, comment header included) with:

```python
# --------------------------------------------------------------------------
# This list moved. The proxy is now default-deny, so the live inventory is
# `websocket_chat._PROXY_ALLOWED_METHODS` itself, and
# tests/unit/routers/test_websocket_chat_method_allowlist.py drives it directly.
# Keeping a second hand-maintained copy here is how it went two months stale.
# --------------------------------------------------------------------------
FRONTEND_RPC_METHODS = sorted(wc._PROXY_ALLOWED_METHODS)
```

Then replace the sibling test at `:175-184` with:

```python
@pytest.mark.parametrize("method", ["chat.abort", "agent.wait", "tools.invoke", "cron.list"])
@pytest.mark.asyncio
async def test_run_adjacent_methods_are_now_denied_by_the_allowlist(monkeypatch, method):
    """These used to forward, as proof the run-dispatch refusal was narrow. They are
    not run-dispatching and they are also not on the proxy allowlist — no client sends
    any of them (aborts ride the typed `agent_abort` frame). So the correct assertion
    is now refusal, and the run-dispatch set is still exactly four."""
    sent, pool, _ = _harness(monkeypatch)

    await _run(method, {"runId": "r1"})

    pool.send_rpc.assert_not_awaited()
    assert sent[-1]["ok"] is False
    assert sent[-1]["error"]["details"]["method"] == method
    assert "use" not in sent[-1]["error"]["details"]  # not the run-dispatch refusal
```

- [ ] **Step 5: Run both files**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_chat_method_allowlist.py tests/unit/routers/test_websocket_chat_run_dispatch_guard.py -v
```

Expected: PASS, all tests.

- [ ] **Step 6: Run the whole websocket test surface**

```bash
cd apps/backend && uv run pytest tests/unit/routers/ -q
```

Expected: PASS. If `test_websocket_chat.py` or `test_websocket_chat_teams.py` fails on a
method the allowlist denies, that method is a real caller the inventory missed — add it to
`_PROXY_ALLOWED_METHODS` with a file:line justification in the commit body.

- [ ] **Step 7: Lint and commit**

```bash
cd apps/backend && uv run ruff check tests/unit/routers/ && uv run ruff format --check tests/unit/routers/
git add apps/backend/tests/unit/routers/test_websocket_chat_method_allowlist.py \
        apps/backend/tests/unit/routers/test_websocket_chat_run_dispatch_guard.py
git commit -m "test(ws): drive the proxy inventory from the allowlist itself"
```

**PR A ends here.** Open the PR, let `claude-review` gate it, merge, watch the dev deploy,
then walk the product on `dev.flickmeon.ai` with Claude-in-Chrome: open chat, send a message,
switch sessions, rename a session, open the Teams panel, open Control → Agents / Skills /
Channels / LLM / Dreams / Sessions, and open the browser panel. Check CloudWatch for
`rpc.refused` with `reason=not_allowlisted` before promoting to prod.

---

## Task 3: Vendor the upstream session-target registry

**Files:**
- Create: `apps/backend/core/services/openclaw_session_targets.py`
- Create: `apps/backend/scripts/vendor_session_target_registry.py`
- Test: `apps/backend/tests/unit/services/test_openclaw_session_targets.py` (create)

**Interfaces:**
- Produces:
  - `core.services.openclaw_session_targets.SOURCE_TAG: str` — the fork git tag, e.g. `"v2026.8.1-isol8.3"`.
  - `core.services.openclaw_session_targets.SESSION_TARGET_FIELDS_BY_METHOD: dict[str, tuple[str, ...]]` — 52 rows.
  - `scripts.vendor_session_target_registry.parse_registry(source: str) -> dict[str, tuple[str, ...]]`
  - `scripts.vendor_session_target_registry.pinned_git_tag() -> str`
  - `scripts.vendor_session_target_registry.render_module(tag: str, registry: dict[str, tuple[str, ...]]) -> str`

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_openclaw_session_targets.py`:

```python
"""Guards on the vendored OpenClaw session-target registry.

Mirrors the vendored-config-schema pattern (tests/chaos/test_schema_staleness.py):
the artifact is pinned to a fork tag, and a plain always-running test fails if the
pin drifts from openclaw-version.json. That is the enforcement for the repo rule
that a base bump must re-vendor in the SAME PR — infra-only PRs skip the backend
suite, so a stale registry would otherwise ship silently.

Unlike the config schema, this artifact needs no fork BUILD: it is parsed straight
out of one TypeScript file at the pinned tag. So the re-vendor is cheap and there is
no reason to let it drift.
"""

import json
from pathlib import Path

from core.services import openclaw_session_targets as reg
from scripts.vendor_session_target_registry import pinned_git_tag

# apps/backend/tests/unit/services/test_openclaw_session_targets.py -> repo root.
# parents: [0]=services [1]=unit [2]=tests [3]=backend [4]=apps [5]=repo root.
_REPO_ROOT = Path(__file__).resolve().parents[5]


def test_registry_tag_matches_the_pinned_fork_base():
    upstream = json.loads((_REPO_ROOT / "openclaw-version.json").read_text())["upstream"]
    assert reg.SOURCE_TAG == pinned_git_tag(), (
        f"vendored session-target registry is from {reg.SOURCE_TAG} but "
        f"openclaw-version.json#upstream is {upstream} — re-run "
        "scripts/vendor_session_target_registry.py --write"
    )


def test_pinned_git_tag_derives_the_v_prefixed_tag_from_the_image_ref():
    """openclaw-version.json stores a full image ref; the fork's git tag is the
    version with a `v`. Pin the derivation so a format change is caught here rather
    than as a mysterious `git show` failure."""
    assert pinned_git_tag().startswith("v")
    upstream = json.loads((_REPO_ROOT / "openclaw-version.json").read_text())["upstream"]
    assert pinned_git_tag() == "v" + upstream.split(":", 1)[1]


def test_registry_carries_the_multi_field_row():
    """sessions.create is the only row with two fields. Our pre-port guard read ONE
    param name, so this row is the proof the port actually changed shape."""
    assert reg.SESSION_TARGET_FIELDS_BY_METHOD["sessions.create"] == ("key", "parentSessionKey")


def test_registry_carries_the_read_only_companion_methods():
    """Upstream's own sessionMutationTargetFields() accessor returns [] for these —
    it asks 'is this a mutation I must authorize', we ask 'is this a cross-tenant
    access'. sessions.companion.ask hands another member's transcript to a model.
    We vendor the RAW map, so they must be present."""
    assert reg.SESSION_TARGET_FIELDS_BY_METHOD["sessions.companion.ask"] == ("sessionKey",)
    assert reg.SESSION_TARGET_FIELDS_BY_METHOD["sessions.companion.state"] == ("sessionKey",)


def test_registry_is_the_expected_size():
    """52 rows at v2026.8.1-isol8.3. A base bump that changes this number is exactly
    the event the drift test in tests/chaos/ exists to explain."""
    assert len(reg.SESSION_TARGET_FIELDS_BY_METHOD) == 52
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_openclaw_session_targets.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.openclaw_session_targets'`.

- [ ] **Step 3: Write the vendoring script**

Create `apps/backend/scripts/vendor_session_target_registry.py`:

```python
#!/usr/bin/env python3
"""Vendor OpenClaw's SESSION_TARGET_FIELDS_BY_METHOD into Python.

The table at fork `src/gateway/session-sharing-target-input.ts` maps every gateway
method that carries a caller-supplied session key to the param name(s) that hold it.
It is upstream's own, maintained by them, and it is the widest such list that exists.
Our RPC proxy needs it to decide "is this key yours?" on the shared per-owner
container, and hand-maintaining a parallel copy is how we ended up guarding 8 of 52.

Unlike the config schema (scripts/vendor_openclaw_schema.sh), this needs NO fork
build — one `git show` at the pinned tag and a regex. So re-vendoring on every base
bump costs seconds, and tests/unit/services/test_openclaw_session_targets.py fails
the build if you skip it.

WHAT THIS DELIBERATELY DOES NOT PORT: upstream's sessionMutationTargetFields()
accessor, which returns [] for READ_ONLY_SESSION_TARGET_METHODS
(sessions.companion.ask / .state). Upstream asks "is this a mutation I must
authorize"; we ask "is this a cross-tenant access". A read of another member's
transcript is exactly what we are stopping, so we take the RAW map.

Usage:
    python3 scripts/vendor_session_target_registry.py --check   # exit 1 on drift
    python3 scripts/vendor_session_target_registry.py --write   # rewrite the module

Requires the fork checkout. Default ~/dev/agent-fork; override with OPENCLAW_FORK.
NEVER checks the fork out to another ref — it reads the pinned tag with `git show`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "apps" / "backend" / "core" / "services" / "openclaw_session_targets.py"
_SOURCE_PATH = "src/gateway/session-sharing-target-input.ts"

# Rows look like ["sessions.patch", ["key"]] ...
_ROW_RE = re.compile(r'\["([A-Za-z][\w.]*)",\s*\[([^\]]*)\]\]')
# ...plus two spread forms: (["a","b"] as const).map((method) => [method, ["x"]] as const)
_SPREAD_RE = re.compile(
    r"\(\s*\[([^\]]*?)\]\s*as const\s*\)\s*\.map\(\(method\)\s*=>\s*\[method,\s*\[([^\]]*)\]\]",
    re.S,
)
_STR_RE = re.compile(r'"([\w.]+)"')


def fork_path() -> Path:
    return Path(os.environ.get("OPENCLAW_FORK", str(Path.home() / "dev" / "agent-fork")))


def pinned_git_tag() -> str:
    """`ghcr.io/…/agent:2026.8.1-isol8.3` -> `v2026.8.1-isol8.3`."""
    upstream = json.loads((_REPO_ROOT / "openclaw-version.json").read_text())["upstream"]
    return "v" + upstream.split(":", 1)[1]


def read_source(tag: str) -> str:
    """Read the registry file AT THE PINNED TAG. The fork working tree is stale."""
    return subprocess.run(
        ["git", "-C", str(fork_path()), "show", f"{tag}:{_SOURCE_PATH}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def parse_registry(source: str) -> dict[str, tuple[str, ...]]:
    """Extract only the SESSION_TARGET_FIELDS_BY_METHOD literal, not the whole file."""
    start = source.index("const SESSION_TARGET_FIELDS_BY_METHOD")
    end = source.index("]);", start)
    body = source[start:end]
    out: dict[str, tuple[str, ...]] = {}
    for method, fields in _ROW_RE.findall(body):
        out[method] = tuple(_STR_RE.findall(fields))
    for methods, fields in _SPREAD_RE.findall(body):
        field_tuple = tuple(_STR_RE.findall(fields))
        for method in _STR_RE.findall(methods):
            out[method] = field_tuple
    if not out:
        raise SystemExit(f"parsed 0 rows from {_SOURCE_PATH} — the upstream shape changed")
    return out


def render_module(tag: str, registry: dict[str, tuple[str, ...]]) -> str:
    rows = "".join(f"    {method!r}: {fields!r},\n" for method, fields in sorted(registry.items()))
    return (
        '"""VENDORED — do not edit by hand.\n'
        "\n"
        "Generated by apps/backend/scripts/vendor_session_target_registry.py from the fork's\n"
        f"{_SOURCE_PATH} at the tag below. Re-run it in the SAME PR as any\n"
        "openclaw-version.json bump; tests/unit/services/test_openclaw_session_targets.py\n"
        "fails the build otherwise.\n"
        '"""\n'
        "\n"
        f"SOURCE_TAG = {tag!r}\n"
        "\n"
        "# method -> the param name(s) carrying a caller-supplied session key.\n"
        "# NOTE: this is the RAW upstream map, NOT sessionMutationTargetFields(), which\n"
        "# blanks the read-only companion methods. See the module docstring of the\n"
        "# generator for why.\n"
        "SESSION_TARGET_FIELDS_BY_METHOD: dict[str, tuple[str, ...]] = {\n"
        f"{rows}"
        "}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="rewrite the vendored module")
    parser.add_argument("--check", action="store_true", help="exit 1 if the module is stale")
    args = parser.parse_args()

    tag = pinned_git_tag()
    rendered = render_module(tag, parse_registry(read_source(tag)))

    if args.write:
        _MODULE_PATH.write_text(rendered)
        print(f"==> Vendored session-target registry for {tag} -> {_MODULE_PATH}")
        return 0

    current = _MODULE_PATH.read_text() if _MODULE_PATH.exists() else ""
    if current != rendered:
        print(f"DRIFT: {_MODULE_PATH} does not match {_SOURCE_PATH} at {tag}", file=sys.stderr)
        print("Re-run: python3 scripts/vendor_session_target_registry.py --write", file=sys.stderr)
        return 1
    print(f"OK: vendored registry matches {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Generate the module**

```bash
cd apps/backend && python3 scripts/vendor_session_target_registry.py --write
```

Expected: `==> Vendored session-target registry for v2026.8.1-isol8.3 -> …/openclaw_session_targets.py`

Verify it produced 52 rows including `'sessions.create': ('key', 'parentSessionKey')`:

```bash
cd apps/backend && python3 -c "
from core.services.openclaw_session_targets import SESSION_TARGET_FIELDS_BY_METHOD as R, SOURCE_TAG
print(SOURCE_TAG, len(R), R['sessions.create'])"
```

Expected: `v2026.8.1-isol8.3 52 ('key', 'parentSessionKey')`

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_openclaw_session_targets.py -v
```

Expected: PASS, 5 tests.

- [ ] **Step 6: Lint and commit**

```bash
cd apps/backend && uv run ruff check core/services/openclaw_session_targets.py scripts/vendor_session_target_registry.py tests/unit/services/test_openclaw_session_targets.py && uv run ruff format --check core/services/openclaw_session_targets.py scripts/vendor_session_target_registry.py tests/unit/services/test_openclaw_session_targets.py
git add apps/backend/core/services/openclaw_session_targets.py \
        apps/backend/scripts/vendor_session_target_registry.py \
        apps/backend/tests/unit/services/test_openclaw_session_targets.py
git commit -m "feat(ws): vendor OpenClaw's session-target registry with a tag-pin guard"
```

---

## Task 4: Guard every key field the registry names, not one

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:1396-1411` (map), `:1498-1523` (guard body)
- Test: `apps/backend/tests/unit/routers/test_websocket_session_key.py`

**Interfaces:**
- Consumes: `core.services.openclaw_session_targets.SESSION_TARGET_FIELDS_BY_METHOD` (Task 3).
- Produces: `websocket_chat._SESSION_KEY_PARAMS_BY_METHOD: dict[str, tuple[str, ...]]`
  (replaces `_SESSION_KEY_PARAM_BY_METHOD`, note the plural) and
  `websocket_chat._PARENT_SESSION_KEY_SENTINELS: frozenset[str]`.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/test_websocket_session_key.py`:

```python
# --------------------------------------------------------------------------
# The vendored-registry union (see docs/superpowers/plans/2026-09-08-rpc-proxy-session-guard.md)
#
# NOTE: this file already imports `AsyncMock, MagicMock, patch` and `pytest` at the
# top (lines 7-9). Do NOT re-import them here — ruff flags F811. Add only these two
# lines up in the existing import block, after the `from routers.websocket_chat import`
# statement:
#
#     from core.services.openclaw_session_targets import SESSION_TARGET_FIELDS_BY_METHOD  # noqa: E402
#     from routers import websocket_chat as wc  # noqa: E402
# --------------------------------------------------------------------------

_MINE = "agent:agent_a:user_1:11111111-1111-4111-8111-111111111111"
_THEIRS = "agent:agent_a:user_2:22222222-2222-4222-8222-222222222222"


async def _guard(method: str, params: dict | None):
    mgmt = AsyncMock()
    with patch.object(wc, "management_api", mgmt):
        consumed = await wc._guard_session_key_ownership(
            user_id="user_1",
            method=method,
            params=params,
            connection_id="conn_1",
            req_id="req_1",
        )
    return consumed


def test_every_registry_row_is_guarded():
    """The whole point of vendoring: no upstream row may be silently unguarded."""
    missing = sorted(set(SESSION_TARGET_FIELDS_BY_METHOD) - set(wc._SESSION_KEY_PARAMS_BY_METHOD))
    assert not missing, f"vendored registry rows with no guard entry: {missing}"


def test_our_own_read_methods_survive_the_union():
    """Upstream's registry models MUTATIONS, so it has no row for these reads. A
    replace-instead-of-union port would silently drop them."""
    for method in ("chat.history", "chat.message.get", "sessions.branches.list"):
        assert wc._SESSION_KEY_PARAMS_BY_METHOD[method] == ("sessionKey",)


@pytest.mark.asyncio
async def test_sessions_create_checks_the_second_field_too():
    """sessions.create carries TWO fields. Guarding only `key` lets a member parent a
    new session onto someone else's transcript."""
    assert wc._SESSION_KEY_PARAMS_BY_METHOD["sessions.create"] == ("key", "parentSessionKey")
    assert await _guard("sessions.create", {"key": _MINE, "parentSessionKey": _THEIRS}) is True
    assert await _guard("sessions.create", {"key": _MINE, "parentSessionKey": _MINE}) is False


@pytest.mark.asyncio
async def test_sessions_create_allows_the_global_parent_sentinels():
    """`global` / `unknown` are upstream's unqualified parent sentinels
    (session-sharing-target-input.ts, `parentUsesRequestedAgent`), not session keys.
    Rejecting them would break the method for everyone, not just an attacker."""
    assert await _guard("sessions.create", {"key": _MINE, "parentSessionKey": "global"}) is False
    assert await _guard("sessions.create", {"key": _MINE, "parentSessionKey": "UNKNOWN"}) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,param",
    [
        ("sessions.fork", "sessionKey"),
        ("sessions.companion.ask", "sessionKey"),
        ("sessions.compaction.restore", "key"),
        ("sessions.assignOwner", "key"),
        ("sessions.goal.update", "sessionKey"),
        ("progressCard.put", "sessionKey"),
    ],
)
async def test_newly_covered_methods_reject_a_foreign_key(method, param):
    assert await _guard(method, {param: _THEIRS}) is True


@pytest.mark.asyncio
async def test_progress_card_get_still_works_on_your_own_session():
    """The one live frontend call site in the newly-guarded set
    (apps/frontend/src/hooks/useProgressCard.ts:40). It sends
    buildChatSessionKey(agentId, userId, sessionId), which is owned."""
    assert await _guard("progressCard.get", {"sessionKey": _MINE}) is False
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py -v -k "registry or union or create or newly or progress"
```

Expected: FAIL with `AttributeError: module 'routers.websocket_chat' has no attribute '_SESSION_KEY_PARAMS_BY_METHOD'`.

- [ ] **Step 3: Replace the map**

In `apps/backend/routers/websocket_chat.py`, add to the imports near `:28`:

```python
from core.services.openclaw_session_targets import SESSION_TARGET_FIELDS_BY_METHOD
```

Replace the whole `_SESSION_KEY_PARAM_BY_METHOD` block (`:1396-1411`, comment header
included) with:

```python
# Methods carrying a CLIENT-SUPPLIED session key that MUST be ownership-checked before
# we forward it to the shared per-owner container.
#
# The bulk is VENDORED from upstream's own SESSION_TARGET_FIELDS_BY_METHOD
# (core/services/openclaw_session_targets.py, regenerated by
# scripts/vendor_session_target_registry.py). We used to hand-maintain 8 of its 52 rows.
# Upstream maintains it as part of their session-sharing authorization, so a base bump
# brings new key-bearing methods with it — which is why the re-vendor is enforced by a
# test rather than by remembering.
#
# It is a UNION, not a replacement: upstream's table models MUTATIONS, so it has no row
# for our transcript READS (chat.history, chat.message.get, sessions.branches.list).
# Those three are ours and must survive every re-vendor.
#
# Values are TUPLES because sessions.create carries two fields (key AND
# parentSessionKey). Every field the table names is checked; checking one is how a
# parent-session bypass would survive.
#
# Depth, not the fence: _PROXY_ALLOWED_METHODS denies all but 44 methods outright, and
# roughly half the dangerous surface carries no session key for this table to see.
_SESSION_KEY_PARAMS_BY_METHOD: dict[str, tuple[str, ...]] = {
    **SESSION_TARGET_FIELDS_BY_METHOD,
    "chat.history": ("sessionKey",),
    "chat.message.get": ("sessionKey",),
    "sessions.branches.list": ("sessionKey",),
}

# Upstream's unqualified parent sentinels for sessions.create — "no specific parent",
# not session keys (session-sharing-target-input.ts, `parentUsesRequestedAgent`).
# Compared lowercased, matching that code.
_PARENT_SESSION_KEY_SENTINELS = frozenset({"global", "unknown"})
```

- [ ] **Step 4: Rewrite the guard's ownership branch**

In `_guard_session_key_ownership`, replace the block from `param_name = _SESSION_KEY_PARAM_BY_METHOD.get(method)`
through `key = foreign[0]` (`:1498-1523`) with:

```python
    param_names = _SESSION_KEY_PARAMS_BY_METHOD.get(method, ())
    list_param_name = _SESSION_KEYS_LIST_PARAM_BY_METHOD.get(method)
    if not param_names and list_param_name is None:
        return False

    key: str | None = None
    if param_names:
        for name in param_names:
            candidate = (params or {}).get(name)
            if not isinstance(candidate, str) or not candidate:
                # No client-supplied key in this field — nothing to validate.
                continue
            if name == "parentSessionKey" and candidate.lower() in _PARENT_SESSION_KEY_SENTINELS:
                continue
            if not _user_owns_session_key(candidate, user_id):
                key = candidate
                break
        if key is None:
            return False
    else:
        keys = (params or {}).get(list_param_name)
        if not isinstance(keys, list) or not keys:
            # Key-less search: the container scans the whole agent; the
            # response post-filter scopes the hits to the caller.
            return False
        foreign = [k for k in keys if not _user_owns_session_key(k, user_id)]
        if not foreign:
            return False
        key = foreign[0]
```

Also update the docstring reference at `:1495` from `_SESSION_KEY_PARAM_BY_METHOD` to
`_SESSION_KEY_PARAMS_BY_METHOD`.

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py tests/unit/routers/test_websocket_chat_permission_mode_guard.py -v
```

Expected: PASS. The `permission_mode` tests must stay green — the forbidden-params branch is
untouched and still runs first.

- [ ] **Step 6: Commit**

```bash
cd apps/backend && uv run ruff check routers/websocket_chat.py tests/unit/routers/test_websocket_session_key.py && uv run ruff format --check routers/websocket_chat.py tests/unit/routers/test_websocket_session_key.py
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_session_key.py
git commit -m "fix(ws): guard every session-key field the vendored registry names"
```

---

## Task 5: The three key-bearing methods upstream's registry omits

Upstream's table exists to authorize session-sharing *mutations*, so it has no row for
`sessions.preview` (a list read), `sessions.viewers.set` (a list write) or `sessions.patchMany`
(handled outside the map). All three are denied by PR A's allowlist today; this closes them
in the guard so they stay closed if anyone ever allowlists them.

**Files:**
- Modify: `apps/backend/routers/websocket_chat.py:1413-1417` (list map), `:1498+` (guard)
- Test: `apps/backend/tests/unit/routers/test_websocket_session_key.py`

**Interfaces:**
- Consumes: `_SESSION_KEY_PARAMS_BY_METHOD`, `_user_owns_session_key` (Task 4).
- Produces: `_SESSION_PATCH_MANY_METHOD: str` constant.

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/unit/routers/test_websocket_session_key.py`:

```python
@pytest.mark.asyncio
async def test_sessions_preview_rejects_a_foreign_key_in_its_list():
    """sessions.preview reads transcript excerpts for up to 64 keys
    (fork sessions-read.ts:540-548). Its `keys` param is a LIST, and the method is
    absent from upstream's registry entirely."""
    assert await _guard("sessions.preview", {"keys": [_MINE, _THEIRS]}) is True
    assert await _guard("sessions.preview", {"keys": [_MINE]}) is False


@pytest.mark.asyncio
async def test_sessions_viewers_set_rejects_a_foreign_key_in_its_list():
    """Also list-shaped (`sessionKeys`, fork sessions-subscriptions.ts:70), also
    absent from the registry."""
    assert await _guard("sessions.viewers.set", {"sessionKeys": [_THEIRS]}) is True
    assert await _guard("sessions.viewers.set", {"sessionKeys": [_MINE]}) is False


@pytest.mark.asyncio
async def test_sessions_patch_many_cannot_route_around_the_sessions_patch_guard():
    """The bypass. Upstream reads targets[].key OUTSIDE the map
    (session-sharing-target-input.ts:271-286), so a verbatim map port misses it and a
    member patches N foreign sessions in one frame."""
    assert await _guard("sessions.patchMany", {"targets": [{"key": _MINE}, {"key": _THEIRS}]}) is True
    assert await _guard("sessions.patchMany", {"targets": [{"key": _MINE}]}) is False


@pytest.mark.asyncio
async def test_sessions_patch_many_with_no_targets_is_left_alone():
    """A malformed frame is upstream's validator's problem, not a security decision."""
    assert await _guard("sessions.patchMany", {}) is False
    assert await _guard("sessions.patchMany", {"targets": []}) is False


@pytest.mark.asyncio
async def test_all_three_are_still_denied_by_the_proxy_allowlist():
    """Depth check: the guard is the second line, the allowlist is the first."""
    for method in ("sessions.preview", "sessions.viewers.set", "sessions.patchMany"):
        assert method not in wc._PROXY_ALLOWED_METHODS
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py -v -k "preview or viewers or patch_many"
```

Expected: FAIL — the first three tests assert `True` but get `False` (the guard returns
`False` for methods it does not know).

- [ ] **Step 3: Extend the list map**

Replace the `_SESSION_KEYS_LIST_PARAM_BY_METHOD` block (`:1413-1417`) with:

```python
# Methods whose client-supplied session keys arrive as a LIST. Every key must be owned
# or the call is refused; a key-less call is scoped by the response post-filter instead
# (``_filter_search_hits_for_user``).
#
# None of the three is in upstream's SESSION_TARGET_FIELDS_BY_METHOD: that table models
# session-sharing MUTATIONS, and a list-shaped read like sessions.preview — which
# returns transcript excerpts for up to 64 keys at once — is not one. Vendoring the
# registry does not cover these; they are ours to maintain.
_SESSION_KEYS_LIST_PARAM_BY_METHOD = {
    "sessions.search": "sessionKeys",
    "sessions.preview": "keys",
    "sessions.viewers.set": "sessionKeys",
}

# sessions.patchMany takes ``targets: [{key, agentId?}, …]`` rather than a key param,
# so neither table above can see it — and upstream handles it the same way, outside
# their map (session-sharing-target-input.ts, ``resolveSessionMutationTargets``).
# Without this it is a one-frame bypass of the sessions.patch guard, against N sessions.
_SESSION_PATCH_MANY_METHOD = "sessions.patchMany"
```

- [ ] **Step 4: Add the `patchMany` branch**

In `_guard_session_key_ownership`, immediately before
`param_names = _SESSION_KEY_PARAMS_BY_METHOD.get(method, ())`, insert:

```python
    if method == _SESSION_PATCH_MANY_METHOD:
        targets = (params or {}).get("targets")
        if not isinstance(targets, list) or not targets:
            return False
        foreign_targets = [
            t.get("key")
            for t in targets
            if isinstance(t, dict)
            and isinstance(t.get("key"), str)
            and not _user_owns_session_key(t["key"], user_id)
        ]
        if not foreign_targets:
            return False
        key = foreign_targets[0]
        logger.warning("[%s] rejected %s targeting non-owned session key=%s", user_id, method, key)
        mgmt = management_api or await get_management_api_client()
        mgmt.send_message(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {"message": "You don't have access to that session."},
            },
        )
        return True
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd apps/backend && uv run pytest tests/unit/routers/test_websocket_session_key.py -v
```

Expected: PASS, all tests in the file.

- [ ] **Step 6: Commit**

```bash
cd apps/backend && uv run ruff check routers/websocket_chat.py tests/unit/routers/test_websocket_session_key.py && uv run ruff format --check routers/websocket_chat.py tests/unit/routers/test_websocket_session_key.py
git add apps/backend/routers/websocket_chat.py apps/backend/tests/unit/routers/test_websocket_session_key.py
git commit -m "fix(ws): guard sessions.preview/viewers.set/patchMany, which upstream's map omits"
```

---

## Task 6: Drift guard — fail when a base bump brings a new key-bearing method

The tag-pin test from Task 3 catches "you bumped and forgot to re-vendor". This catches
the subtler one: you re-vendored, the tag matches, and the new tag added a method our
guard has no entry for.

**Files:**
- Test: `apps/backend/tests/chaos/test_session_target_registry_drift.py` (create)

**Interfaces:**
- Consumes: `scripts.vendor_session_target_registry.{parse_registry, read_source, pinned_git_tag, fork_path}`,
  `websocket_chat._SESSION_KEY_PARAMS_BY_METHOD`.
- Produces: nothing.

- [ ] **Step 1: Write the test**

Create `apps/backend/tests/chaos/test_session_target_registry_drift.py`:

```python
"""The vendored registry must match the fork, and the guard must cover all of it.

Two failure modes, one file:

1. Someone edits core/services/openclaw_session_targets.py by hand, or the fork's
   table changes shape and the regex silently parses fewer rows.
2. A base bump adds a key-bearing method and the guard gains no entry for it — the
   exact drift that left us guarding 8 of 52 rows.

Skips without a fork checkout, which is CI's normal state. That is deliberate: the
guard that MUST hold in CI is the cheap tag-pin assertion in
tests/unit/services/test_openclaw_session_targets.py. This one is for the human
doing the base bump, on the machine that has the fork.

NEVER `git checkout` the fork. read_source() uses `git show <tag>:<path>`; the fork's
working tree is a stale 2026.7.2 and does not reflect what we run.
"""

import pytest

from routers import websocket_chat as wc
from scripts.vendor_session_target_registry import (
    fork_path,
    parse_registry,
    pinned_git_tag,
    read_source,
)


def _registry_at_pinned_tag():
    if not (fork_path() / ".git").exists():
        pytest.skip(f"fork checkout absent at {fork_path()} (set OPENCLAW_FORK)")
    return parse_registry(read_source(pinned_git_tag()))


@pytest.mark.chaos
def test_vendored_module_matches_the_fork_at_the_pinned_tag():
    from core.services.openclaw_session_targets import SESSION_TARGET_FIELDS_BY_METHOD as vendored

    live = _registry_at_pinned_tag()
    assert vendored == live, (
        "core/services/openclaw_session_targets.py is out of sync with the fork at "
        f"{pinned_git_tag()}. Re-run:\n"
        "  python3 apps/backend/scripts/vendor_session_target_registry.py --write"
    )


@pytest.mark.chaos
def test_every_key_bearing_method_at_the_pinned_tag_has_a_guard_entry():
    live = _registry_at_pinned_tag()
    guarded = set(wc._SESSION_KEY_PARAMS_BY_METHOD) | set(wc._SESSION_KEYS_LIST_PARAM_BY_METHOD)
    guarded.add(wc._SESSION_PATCH_MANY_METHOD)
    missing = sorted(set(live) - guarded)
    assert not missing, (
        f"the fork at {pinned_git_tag()} carries session-key-bearing methods with no guard "
        f"entry: {missing}. Re-vendor, then confirm each one is either denied by "
        "_PROXY_ALLOWED_METHODS or covered by _SESSION_KEY_PARAMS_BY_METHOD."
    )


@pytest.mark.chaos
def test_nothing_key_bearing_is_both_allowlisted_and_unguarded():
    """The composition rule between the two mechanisms: a method the proxy will forward
    AND that carries a session key MUST have a guard entry. This is the assertion that
    fails the day someone allowlists sessions.fork for a new UI."""
    live = _registry_at_pinned_tag()
    guarded = set(wc._SESSION_KEY_PARAMS_BY_METHOD) | set(wc._SESSION_KEYS_LIST_PARAM_BY_METHOD)
    guarded.add(wc._SESSION_PATCH_MANY_METHOD)
    holes = sorted((set(live) & wc._PROXY_ALLOWED_METHODS) - guarded)
    assert not holes, f"allowlisted, key-bearing, and unguarded: {holes}"
```

- [ ] **Step 2: Run it with the fork present**

```bash
cd apps/backend && OPENCLAW_FORK=$HOME/dev/agent-fork uv run pytest tests/chaos/test_session_target_registry_drift.py -v
```

Expected: PASS, 3 tests.

- [ ] **Step 3: Run it as CI will (no fork)**

```bash
cd apps/backend && OPENCLAW_FORK=/nonexistent uv run pytest tests/chaos/test_session_target_registry_drift.py -v
```

Expected: 3 SKIPPED with "fork checkout absent".

- [ ] **Step 4: Run the full backend suite once**

```bash
cd apps/backend && uv run pytest tests/ -q
```

Expected: PASS. This is the one full run — per the repo rule, iterate with targeted tests
and let CI carry the rest.

- [ ] **Step 5: Lint and commit**

```bash
cd apps/backend && uv run ruff check tests/chaos/test_session_target_registry_drift.py && uv run ruff format --check tests/chaos/test_session_target_registry_drift.py
git add apps/backend/tests/chaos/test_session_target_registry_drift.py
git commit -m "test(ws): fail when a base bump brings an unguarded key-bearing method"
```

**PR B ends here.**

---

## Post-merge checklist

- [ ] PR A merged → dev deploy green → Claude-in-Chrome walk on `dev.flickmeon.ai` (chat send,
      session switch/rename/delete, branch switch, Teams panel, Control → Agents / Skills /
      Channels / LLM / Dreams / Sessions, browser panel).
- [ ] CloudWatch: no `rpc.refused` with `reason=not_allowlisted` for a method the UI needs.
      If one appears, add it to `_PROXY_ALLOWED_METHODS` with its `file:line` before promoting.
- [ ] PR B merged → dev deploy green → repeat the chat + progress-card walk (the one live
      call site the registry port newly guards).
- [ ] **One** prod promotion at the end: `gh workflow run deploy.yml --ref main`. Watch it with
      `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`.
- [ ] Confirm prod actually moved — read the running task definition's image, not `main`:
      `aws ecs describe-task-definition --profile isol8-admin …`. Merging does not ship.
- [ ] `/graphify . --update` once both PRs land.
- [ ] Delete this plan file (repo rule: shipped plans are deleted, git history is the record).

## Not in scope

- **`browser.request`** is on the allowlist because the browser panel needs it
  (`BrowserDockPanel.tsx:58`), but it proxies an arbitrary `{method, path}` to the container's
  browser service under `operator.admin`. That is its own finding; it is not made worse by
  anything here.
- **The desktop node relay and the Paperclip operator relay.** Different principals, different
  code paths, both diverted before `_process_rpc_background`. Allowlisting either would break
  the desktop app or every Teams run.
- **`_FORBIDDEN_PARAMS_BY_METHOD` / `permissionMode`** — shipped in PR #2348. Untouched here.
