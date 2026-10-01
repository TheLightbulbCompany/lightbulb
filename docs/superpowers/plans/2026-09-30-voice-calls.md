# Voice Calls Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workspace member taps **Call** on an agent's DM (web, desktop web view, iPhone) and talks to it full-duplex through OpenAI `gpt-live-1`; real work is delegated to that Hermes agent as call-scoped turns whose progress streams back to the voice.

**Architecture:** The backend brokers the WebRTC SDP exchange with OpenAI (key never leaves the backend), budgets and holds the call's cost, and closes + bills every call through one seam (`close_call`) reached by hang-up or a one-minute sweeper. Delegations become call runs on the bridge (own routes and session key, the same environment a DM turn gets, sharing the run registry that abort uses); the bridge publishes `call.turn.frame` events straight to the caller's private AppSync user channel. A platform-neutral `VoiceCallSession` in `@lightbulb/shared` runs the delegation loop for web and iPhone.

**Tech Stack:** FastAPI + DynamoDB (boto3) + httpx + `websockets` 16 (backend); Python bridge (Hermes image); TypeScript `@lightbulb/shared` (vitest); Next.js 16 (vitest); Expo 57 / RN 0.86 + `react-native-webrtc` (jest-expo); Tauri plists; AWS CDK.

**Spec:** `docs/superpowers/specs/2026-09-30-voice-calls-design.md` — read it before any task.

## Global Constraints

- Model `gpt-live-1`, endpoint `POST https://api.openai.com/v1/live/sessions`, sideband `wss://api.openai.com/v1/live/sessions/{id}/attach`, voice `marin`, `delegation: {"type": "client"}`, data channel `oai-events`.
- SDP is passed byte-exact (never `.strip()` it).
- Free allowance: **600 seconds per workspace per UTC day** (`trial_cap.utc_today()`). Max call **1800 s**. Minimum budget to start **60 s**.
- Credit pricing = provider cost × `OPENROUTER_MARKUP`; provider cost = **$0.05/min**. The markup's only copy is `openrouter-pricing.json`.
- Credits mode ⇔ stored `provider_choice == "openrouter_managed"` **and** not `trial_cap.is_managed_capped(account)`. Never use `provider_choice_from_row`.
- Expired trial / canceled / past_due / no billing row → 402 `voice_limit` `{reason: "plan_inactive"}`.
- `OPENAI_API_KEY` empty → 503 `{code: "voice_unavailable"}`. Everything ships dark until the owner sets the key.
- A call leaves nothing behind: the call row is deleted at close; no transcript is stored.
- Owner rule: **CI is the test gate** — do not run full suites locally; write the tests, push, read CI. (A single targeted test file is fine while iterating.)
- Work in the worktree `~/Desktop/lightbulb-voice-calls.nosync` (branch off `origin/main`); never stack on another agent's branch.
- Any change under `apps/infra/lib/` requires `cd apps/infra && npx cdk synth` and committing the regenerated `.github/workflows/deploy.yml` in the same PR.
- Web colours come from tokens (`--plane-raised`, `--fg`, `--fg-soft`, `--alert`, …); no colour literals (`brandTokens.test.ts`).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR bodies end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## Review Focus

1. **Two closers race** (hang-up while the sweeper fires): exactly one bill, hold released once. → Task 4 test `test_close_call_loser_does_nothing`.
2. **Sideband attach fails** (tab closed, network died): call still closes, bills by the fallback rule, emits `voice.usage.fallback`, row deleted. → Task 4 test `test_close_call_attach_failure_bills_fallback`.
3. **A new delegation while the previous turn runs**: the old run is stopped and its frames are ignored by the client. → Task 5 test `test_turn_supersedes_previous_run`; Task 9 test `drops frames for a superseded turn`.
4. **Someone else's call id** (another member of the workspace, or another workspace): 404, no turn started, no close. → Task 5 test `test_turn_rejects_other_user`.
5. **Credits workspace with no fast-gate row / incomplete ledger**: refused 402 `credits_low`, never granted 30 free minutes. → Task 2 test `test_credits_budget_zero_without_row`.

---

## PR 1 — server side (backend + bridge + shared event type), dark until the key exists

### Task 1: Config, secret and markup wiring

**Files:**
- Modify: `apps/backend/core/config.py` (next to `METRONOME_API_KEY`)
- Modify: `apps/infra/lib/stacks/auth-stack.ts` (`this.secrets = { … }`)
- Modify: `apps/infra/lib/stacks/service-stack.ts` (props `secretNames`, container `secrets:` map, container `environment:` map, the stale Metronome comment at ~L1577)
- Modify: wherever `secretNames` is assembled from `authStack.secrets` (`apps/infra/lib/lightbulb-stage.ts` — `grep -n "metronomeApiKey" apps/infra/lib`)
- Modify: `.github/workflows/deploy.yml` (regenerated)
- Test: `apps/infra/test/` existing service-stack test file (`grep -ln "METRONOME_API_KEY" apps/infra/test`)

**Interfaces:**
- Produces: `settings.OPENAI_API_KEY: str`, `settings.OPENROUTER_MARKUP: str` (e.g. `"1.2"`), secret `lightbulb/{env}/openai_api_key`.

- [ ] **Step 1: Write the failing infra test** — in the service-stack test file, next to the existing `METRONOME_API_KEY` assertion, add:

```ts
it("wires the voice key and the inference markup into the backend task", () => {
  const container = backendContainerDefinition(); // use the file's existing helper that returns the backend ContainerDefinition
  expect(container.Secrets.map((s: { Name: string }) => s.Name)).toContain("OPENAI_API_KEY");
  const env = Object.fromEntries(container.Environment.map((e: { Name: string; Value: string }) => [e.Name, e.Value]));
  expect(env.OPENROUTER_MARKUP).toBe(JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "..", "openrouter-pricing.json"), "utf8")).markup);
});
```

If the file has no such helper, copy the lookup its `METRONOME_API_KEY` test uses verbatim.

- [ ] **Step 2: Backend setting** — in `core/config.py` after `METRONOME_API_KEY`:

```python
    OPENAI_API_KEY: str = ""
    """Platform key for GPT-Live voice sessions. From Secrets Manager. Empty ⇒ calls 503 voice_unavailable."""
    OPENROUTER_MARKUP: str = ""
    """Managed-credit markup (repo-root openrouter-pricing.json, injected by CDK). Voice debits use it."""
```

- [ ] **Step 3: Secret** — `auth-stack.ts`, inside `this.secrets = {`:

```ts
      openaiApiKey: createSecret("OpenaiApiKey", "openai_api_key"),
```

Add `openaiApiKey: string;` to `secretNames` in `service-stack.ts` props and pass `openaiApiKey: authStack.secrets.openaiApiKey.secretName` wherever `metronomeApiKey` is passed (same expression shape as its neighbour).

- [ ] **Step 4: Task def** — `service-stack.ts` `secrets:` map, after `METRONOME_API_KEY`:

```ts
        OPENAI_API_KEY: ecs.Secret.fromSecretsManager(
          secretsmanager.Secret.fromSecretNameV2(this, "ImportOpenaiApiKey", props.secretNames.openaiApiKey),
        ),
```

`environment:` map, next to `OPENROUTER_WORKSPACE_ID`:

```ts
        // Voice debits reuse the managed-credit markup; its ONE copy is openrouter-pricing.json.
        OPENROUTER_MARKUP: JSON.parse(
          fs.readFileSync(path.join(__dirname, "..", "..", "..", "..", "openrouter-pricing.json"), "utf8"),
        ).markup,
```

(Add `import * as fs from "fs"; import * as path from "path";` if absent.) Replace the stale comment at ~L1577 "…it never ingests usage (the meter Lambda does)…" with "…ingests voice-call usage (voice calls); the openrouter-settlement Lambda ingests inference usage…".

- [ ] **Step 5: Regenerate the pipeline** — `cd apps/infra && npx cdk synth > /dev/null`; `git add .github/workflows/deploy.yml`.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/config.py apps/infra/lib apps/infra/test .github/workflows/deploy.yml
git commit -m "feat(voice): OpenAI key secret + markup env for the backend

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Voice billing primitives

**Files:**
- Create: `apps/backend/core/billing/voice_billing.py`
- Test: `apps/backend/tests/unit/billing/test_voice_billing.py`

**Interfaces:**
- Consumes: `billing_repo.get_by_owner_id`, `trial_cap.is_managed_capped`, `trial_cap.is_trial_expired`, `trial_cap.utc_today`, `metronome_gate.is_owner_blocked`, `metronome_gate.RESERVE_ESTIMATE_MICROCENTS`, `MetronomeFastGate().get_row`, `container_repo._get_table`, `settings.OPENROUTER_MARKUP`.
- Produces:
  - `FREE_SECONDS_PER_DAY = 600`, `MAX_CALL_SECONDS = 1800`, `MIN_CALL_SECONDS = 60`
  - `def billing_mode(account: dict | None) -> Literal["credits", "free"] | None`
  - `def provider_nanodollars(seconds: int) -> int`
  - `def charge_microcents(seconds: int) -> int`
  - `async def budget_seconds(owner_id: str, mode: str) -> int` (already capped at `MAX_CALL_SECONDS`)
  - `FREE_KEY_PREFIX = "__voice__#"`

- [ ] **Step 1: Write the failing tests**

```python
"""Voice billing: who pays, how much a call may last, what a second costs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.billing import voice_billing as vb

ACTIVE_CREDITS = {"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"}


@pytest.mark.parametrize(
    "account, expected",
    [
        (None, None),
        ({"subscription_status": "canceled", "plan": "standard"}, None),
        ({"subscription_status": "past_due", "plan": "base"}, None),
        (ACTIVE_CREDITS, "credits"),
        ({"subscription_status": "active", "plan": "base"}, "free"),
        ({"subscription_status": "active", "plan": "standard"}, "free"),  # no provider chosen = capped
        ({"subscription_status": "active", "plan": "standard", "provider_choice": "byo_key"}, "free"),
        ({"subscription_status": "active", "plan": "ultra_pro", "provider_choice": "chatgpt_oauth"}, "free"),
        ({"subscription_status": "trial_local", "trial_end": 4102444800}, "free"),
    ],
)
def test_billing_mode(account, expected):
    assert vb.billing_mode(account) == expected


def test_billing_mode_expired_trial_is_refused():
    with patch.object(vb.trial_cap, "is_trial_expired", return_value=True):
        assert vb.billing_mode({"subscription_status": "trial_local", "trial_end": 1}) is None


def test_prices_follow_provider_cost_times_markup():
    assert vb.provider_nanodollars(60) == 50_000_000  # $0.05
    with patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"):
        assert vb.charge_microcents(60) == 60_000  # 6 cents
        assert vb.charge_microcents(1) == 1_000  # ceil, never free


def test_charge_refuses_unset_markup():
    with patch.object(vb.settings, "OPENROUTER_MARKUP", ""), pytest.raises(ValueError):
        vb.charge_microcents(60)


@pytest.mark.asyncio
async def test_free_budget_reads_todays_counter():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"day": "2026-09-30", "usedSeconds": 450}}
    with patch.object(vb, "_containers", return_value=table), patch.object(vb.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await vb.budget_seconds("org_a", "free") == 150


@pytest.mark.asyncio
async def test_free_budget_resets_on_a_new_day():
    table = MagicMock()
    table.get_item.return_value = {"Item": {"day": "2026-09-29", "usedSeconds": 600}}
    with patch.object(vb, "_containers", return_value=table), patch.object(vb.trial_cap, "utc_today", return_value="2026-09-30"):
        assert await vb.budget_seconds("org_a", "free") == 600


@pytest.mark.asyncio
async def test_credits_budget_uses_the_gate_headroom_rule():
    row = {"remaining_microcents": 300_000 + 60_000 * 5, "reserved_microcents": 0, "openrouter_reserved_microcents": 60_000}
    gate = MagicMock(get_row=AsyncMock(return_value=row))
    with (
        patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=False)),
        patch.object(vb, "MetronomeFastGate", return_value=gate),
        patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"),
    ):
        assert await vb.budget_seconds("org_a", "credits") == 240  # 4 minutes of headroom at 6¢/min


@pytest.mark.asyncio
async def test_credits_budget_zero_without_row():
    gate = MagicMock(get_row=AsyncMock(return_value=None))
    with (
        patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=False)),
        patch.object(vb, "MetronomeFastGate", return_value=gate),
        patch.object(vb.settings, "OPENROUTER_MARKUP", "1.2"),
    ):
        assert await vb.budget_seconds("org_a", "credits") == 0


@pytest.mark.asyncio
async def test_credits_budget_zero_when_blocked():
    with patch.object(vb.metronome_gate, "is_owner_blocked", AsyncMock(return_value=True)):
        assert await vb.budget_seconds("org_a", "credits") == 0


@pytest.mark.asyncio
async def test_budget_is_capped_at_thirty_minutes():
    table = MagicMock()
    table.get_item.return_value = {}
    with patch.object(vb, "_containers", return_value=table), patch.object(vb, "FREE_SECONDS_PER_DAY", 99_999):
        assert await vb.budget_seconds("org_a", "free") == vb.MAX_CALL_SECONDS
```

- [ ] **Step 2: Implement** `core/billing/voice_billing.py`:

```python
"""Voice-call billing primitives: who pays, how long a call may run, what a second costs.

Credits mode mirrors the openrouter-settlement Lambda: provider cost is ingested to Metronome
and priced there at the managed-credit markup; the fast-gate row gets the same provisional
debit settlement applies (the next balance refresh overwrites it from Metronome). Free mode is
a per-workspace daily seconds counter in the containers table (``__voice__#<owner>``).
"""

from __future__ import annotations

import math
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services import metronome_gate, trial_cap

FREE_SECONDS_PER_DAY = 600
MAX_CALL_SECONDS = 1800
MIN_CALL_SECONDS = 60
FREE_KEY_PREFIX = "__voice__#"
VOICE_PROVIDER_USD_PER_MINUTE = Decimal("0.05")
_NANODOLLARS_PER_USD = Decimal(10**9)
_LIVE_STATUSES = {"active", "trialing", "trial_local"}


def _containers():
    return get_table("containers")


def billing_mode(account: dict | None) -> Literal["credits", "free"] | None:
    """None = no voice (no row, lapsed, expired trial). Stored fields only — never
    provider_choice_from_row, which reports openrouter_managed for trial_local."""
    if not account or account.get("subscription_status") not in _LIVE_STATUSES:
        return None
    if account.get("subscription_status") == "trial_local" and trial_cap.is_trial_expired(account):
        return None
    if account.get("provider_choice") == "openrouter_managed" and not trial_cap.is_managed_capped(account):
        return "credits"
    return "free"


def provider_nanodollars(seconds: int) -> int:
    return int((Decimal(seconds) * VOICE_PROVIDER_USD_PER_MINUTE * _NANODOLLARS_PER_USD / 60).to_integral_value(rounding=ROUND_CEILING))


def charge_microcents(seconds: int) -> int:
    """Same formula as openrouter-settlement ``debit_microcents``: cost × markup, rounded up."""
    markup = Decimal(settings.OPENROUTER_MARKUP or "0")
    if not markup.is_finite() or markup < 1:
        raise ValueError("OPENROUTER_MARKUP must be a decimal >= 1")
    return int((Decimal(provider_nanodollars(seconds)) * markup / 1000).to_integral_value(rounding=ROUND_CEILING))


async def budget_seconds(owner_id: str, mode: str) -> int:
    if mode == "free":
        item = (await run_in_thread(_containers().get_item, Key={"owner_id": f"{FREE_KEY_PREFIX}{owner_id}"}, ConsistentRead=True)).get("Item") or {}
        used = int(item.get("usedSeconds", 0)) if item.get("day") == trial_cap.utc_today() else 0
        return max(0, min(FREE_SECONDS_PER_DAY - used, MAX_CALL_SECONDS))
    if await metronome_gate.is_owner_blocked(owner_id):
        return 0
    row = await MetronomeFastGate().get_row(owner_id)
    if not row or row.get("remaining_microcents") is None:
        return 0
    headroom = (
        int(row["remaining_microcents"])
        - int(row.get("reserved_microcents", 0))
        - int(row.get("openrouter_reserved_microcents", 0))
        - metronome_gate.RESERVE_ESTIMATE_MICROCENTS
    )
    per_minute = charge_microcents(60)
    return max(0, min(math.floor(headroom * 60 / per_minute), MAX_CALL_SECONDS))
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/billing/voice_billing.py apps/backend/tests/unit/billing/test_voice_billing.py
git commit -m "feat(voice): billing mode, budget and pricing primitives

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: OpenAI GPT-Live client

**Files:**
- Create: `apps/backend/core/services/openai_live.py`
- Test: `apps/backend/tests/unit/services/test_openai_live.py`

**Interfaces:**
- Produces:
  - `class LiveUnavailable(Exception)` (no key), `class LiveUpstreamError(Exception)`
  - `async def create_session(*, sdp: str, instructions: str) -> tuple[str, str]` → `(session_id, answer_sdp)`
  - `async def close_session(session_id: str) -> int | None` → billed seconds, or `None` when the sideband could not attach / never saw `session.closed`.

- [ ] **Step 1: Write the failing tests**

```python
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.services import openai_live


@pytest.mark.asyncio
async def test_create_session_refuses_without_key():
    with patch.object(openai_live.settings, "OPENAI_API_KEY", ""), pytest.raises(openai_live.LiveUnavailable):
        await openai_live.create_session(sdp="v=0\r\n", instructions="x")


@pytest.mark.asyncio
async def test_create_session_passes_sdp_byte_exact():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201, json={"session": {"id": "live_1"}, "transport": {"type": "webrtc", "sdp": "answer\r\n"}})

    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"), patch.object(openai_live, "_transport", httpx.MockTransport(handler)):
        session_id, answer = await openai_live.create_session(sdp="offer\r\n", instructions="be brief")
    assert (session_id, answer) == ("live_1", "answer\r\n")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["transport"] == {"type": "webrtc", "sdp": "offer\r\n"}
    assert seen["body"]["session"]["model"] == "gpt-live-1"
    assert seen["body"]["session"]["delegation"] == {"type": "client"}
    assert seen["body"]["session"]["audio"] == {"output": {"voice": "marin"}}


@pytest.mark.asyncio
async def test_create_session_raises_on_non_201():
    transport = httpx.MockTransport(lambda _r: httpx.Response(400, json={"error": "bad sdp"}))
    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"), patch.object(openai_live, "_transport", transport), pytest.raises(openai_live.LiveUpstreamError):
        await openai_live.create_session(sdp="x", instructions="y")


class _FakeSocket:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        return json.dumps(self.incoming.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
async def test_close_session_returns_billed_seconds():
    sock = _FakeSocket([{"type": "session.usage.updated", "usage": {"seconds": 12}}, {"type": "session.closed", "reason": "close_requested", "usage": {"seconds": 15.2}}])
    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"), patch.object(openai_live, "_connect", MagicMock(return_value=sock)):
        assert await openai_live.close_session("live_1") == 16  # rounded up
    assert sock.sent[0]["type"] == "session.close"


@pytest.mark.asyncio
async def test_close_session_returns_none_when_attach_fails():
    def boom(*_a, **_k):
        raise OSError("gone")

    with patch.object(openai_live.settings, "OPENAI_API_KEY", "sk-test"), patch.object(openai_live, "_connect", boom):
        assert await openai_live.close_session("live_1") is None
```

- [ ] **Step 2: Implement** `core/services/openai_live.py`:

```python
"""GPT-Live session broker. The backend creates every session (the client sends its SDP offer
here; the key never leaves the backend) and ends every session through the sideband, whose
``session.closed`` carries the authoritative billed seconds."""

from __future__ import annotations

import asyncio
import json
import math
import uuid

import httpx
from websockets.asyncio.client import connect as _ws_connect

from core.config import settings

_BASE = "https://api.openai.com/v1/live/sessions"
_SIDEBAND = "wss://api.openai.com/v1/live/sessions/{session_id}/attach"
_CLOSE_WAIT_S = 15.0
_transport: httpx.AsyncBaseTransport | None = None  # tests inject a MockTransport


class LiveUnavailable(Exception):
    """No platform key configured."""


class LiveUpstreamError(Exception):
    """OpenAI refused or failed the session create."""


def _key() -> str:
    if not settings.OPENAI_API_KEY:
        raise LiveUnavailable()
    return settings.OPENAI_API_KEY


async def create_session(*, sdp: str, instructions: str) -> tuple[str, str]:
    body = {
        "session": {
            "model": "gpt-live-1",
            "instructions": instructions,
            "audio": {"output": {"voice": "marin"}},
            "delegation": {"type": "client"},
        },
        "transport": {"type": "webrtc", "sdp": sdp},  # byte-exact: a stripped CRLF 400s
    }
    async with httpx.AsyncClient(timeout=20.0, transport=_transport) as client:
        resp = await client.post(_BASE, json=body, headers={"Authorization": f"Bearer {_key()}"})
    if resp.status_code != 201:
        raise LiveUpstreamError(f"live session create returned {resp.status_code}")
    data = resp.json()
    return data["session"]["id"], data["transport"]["sdp"]


def _connect(session_id: str):
    return _ws_connect(_SIDEBAND.format(session_id=session_id), additional_headers={"Authorization": f"Bearer {_key()}"}, open_timeout=10)


async def close_session(session_id: str) -> int | None:
    try:
        async with _connect(session_id) as ws:
            await ws.send(json.dumps({"type": "session.close", "event_id": f"evt_{uuid.uuid4().hex}"}))
            async with asyncio.timeout(_CLOSE_WAIT_S):
                while True:
                    event = json.loads(await ws.recv())
                    if event.get("type") == "session.closed":
                        return math.ceil(float((event.get("usage") or {}).get("seconds") or 0))
    except Exception:  # noqa: BLE001 -- attach refused, socket died or timed out: caller bills by fallback
        return None
```

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/openai_live.py apps/backend/tests/unit/services/test_openai_live.py
git commit -m "feat(voice): GPT-Live session create + sideband close

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Call store and `close_call`

**Files:**
- Create: `apps/backend/core/services/voice_calls.py`
- Test: `apps/backend/tests/unit/services/test_voice_calls_close.py`

**Interfaces:**
- Consumes: Task 2 (`voice_billing.*`), Task 3 (`openai_live.close_session`), `MetronomeClient` via `metronome_client.for_settings`, `MetronomeFastGate`, `container_repo._get_table`, `put_metric`.
- Produces:
  - `CALL_KEY_PREFIX = "__call__#"`, statuses `"call_open"`, `"call_closing"`
  - `async def put_call(call: dict) -> None` (call dict keys: `callId, ownerId, userId, agentId, companyId, sessionId, startedAt:int, deadline:int, billing, holdMicrocents:int, status, turnRunId|None`)
  - `async def get_call(call_id: str) -> dict | None`
  - `async def set_turn(call_id: str, run_id: str) -> None`
  - `async def close_call(call_id: str, *, now: int | None = None) -> bool` (True if this caller closed it)
  - `async def release_hold(owner_id: str, hold: int) -> None`

- [ ] **Step 1: Write the failing tests**

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from core.services import voice_calls as vc

CALL = {
    "owner_id": "__call__#call-1", "callId": "call-1", "ownerId": "org_a", "userId": "user_a", "agentId": "agent-1",
    "companyId": "co-1", "sessionId": "live_1", "startedAt": 1000, "deadline": 1600, "billing": "credits",
    "holdMicrocents": 36_000, "status": "call_open", "turnRunId": None,
}


def _conditional_fail():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")


def _patches(*, claim_ok=True, seconds=90, call=CALL):
    table = MagicMock()
    table.get_item.return_value = {"Item": dict(call)}
    if claim_ok:
        table.update_item.return_value = {"Attributes": {**call, "status": "call_closing"}}
    else:
        table.update_item.side_effect = _conditional_fail()
    table.name = "lightbulb-dev-containers"
    client = MagicMock()
    table.meta.client = client
    metronome = MagicMock(ingest=AsyncMock())
    return table, client, metronome, [
        patch.object(vc, "_table", return_value=table),
        patch.object(vc.openai_live, "close_session", AsyncMock(return_value=seconds)),
        patch.object(vc, "_metronome", return_value=metronome),
        patch.object(vc, "_fast_gate_table_name", return_value="lightbulb-dev-metronome-fast-gate"),
        patch.object(vc.voice_billing.settings, "OPENROUTER_MARKUP", "1.2"),
        patch.object(vc, "_stop_turn", AsyncMock()),
    ]


async def _run(patches, coro):
    for p in patches:
        p.start()
    try:
        return await coro
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_close_call_bills_credits_once_and_deletes_row():
    table, client, metronome, patches = _patches(seconds=90)
    assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    event = metronome.ingest.await_args.args[0][0]
    assert event["event_type"] == "voice_call"
    assert event["transaction_id"] == "call-1"
    assert event["customer_id"] == "org_a"
    assert event["properties"]["provider_cost_nanodollars"] == "75000000"  # 90 s × $0.05/min
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    gate = items[0]["Update"]
    assert gate["TableName"] == "lightbulb-dev-metronome-fast-gate"
    assert gate["ExpressionAttributeValues"][":debit"] == {"N": "-90000"}
    assert gate["ExpressionAttributeValues"][":hold"] == {"N": "36000"}
    assert items[1]["Delete"]["Key"] == {"owner_id": {"S": "__call__#call-1"}}


@pytest.mark.asyncio
async def test_close_call_loser_does_nothing():
    table, client, metronome, patches = _patches(claim_ok=False)
    assert await _run(patches, vc.close_call("call-1", now=1100)) is False
    metronome.ingest.assert_not_awaited()
    client.transact_write_items.assert_not_called()


@pytest.mark.asyncio
async def test_close_call_attach_failure_bills_fallback():
    table, client, metronome, patches = _patches(seconds=None)
    with patch.object(vc, "put_metric") as metric:
        assert await _run(patches, vc.close_call("call-1", now=1100)) is True
    metric.assert_any_call("voice.usage.fallback")
    event = metronome.ingest.await_args.args[0][0]
    assert event["properties"]["seconds"] == "100"  # min(now, deadline) − startedAt


@pytest.mark.asyncio
async def test_close_call_free_counts_seconds_in_one_transaction():
    table, client, metronome, patches = _patches(seconds=45, call={**CALL, "billing": "free", "holdMicrocents": 0})
    with patch.object(vc.voice_billing.trial_cap, "utc_today", return_value="2026-09-30"):
        await _run(patches, vc.close_call("call-1", now=1100))
    metronome.ingest.assert_not_awaited()
    items = client.transact_write_items.call_args.kwargs["TransactItems"]
    counter = items[0]["Update"]
    assert counter["Key"] == {"owner_id": {"S": "__voice__#org_a"}}
    assert counter["ExpressionAttributeValues"][":s"] == {"N": "45"}
    assert items[1]["Delete"]["Key"] == {"owner_id": {"S": "__call__#call-1"}}
```

- [ ] **Step 2: Implement** `core/services/voice_calls.py` (store + close; the start/turn code lands in Task 5 in the same module):

```python
"""Voice calls: one containers-table row per live call, and the ONE close seam.

A call leaves nothing behind: ``close_call`` bills and deletes the row in one transaction.
The row sits on the sparse ``status-index`` while ``call_open`` / ``call_closing`` so the
sweeper finds it with one query.
"""

from __future__ import annotations

import logging
import time

import httpx
from botocore.exceptions import ClientError

from core.billing import metronome_client, voice_billing
from core.billing import metronome_fast_gate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.services import openai_live

logger = logging.getLogger(__name__)

CALL_KEY_PREFIX = "__call__#"
OPEN = "call_open"
CLOSING = "call_closing"


def _table():
    return get_table("containers")


def _key(call_id: str) -> dict:
    return {"owner_id": f"{CALL_KEY_PREFIX}{call_id}"}


def _fast_gate_table_name() -> str:
    return metronome_fast_gate._table().name


def _metronome(http: httpx.AsyncClient):
    return metronome_client.for_settings(http)


async def put_call(call: dict) -> None:
    await run_in_thread(_table().put_item, Item={**_key(call["callId"]), **call}, ConditionExpression="attribute_not_exists(owner_id)")


async def get_call(call_id: str) -> dict | None:
    return (await run_in_thread(_table().get_item, Key=_key(call_id), ConsistentRead=True)).get("Item")


async def set_turn(call_id: str, run_id: str) -> None:
    await run_in_thread(
        _table().update_item, Key=_key(call_id), UpdateExpression="SET turnRunId = :r",
        ConditionExpression="#s = :open", ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":r": run_id, ":open": OPEN},
    )


async def release_hold(owner_id: str, hold: int) -> None:
    """Used only when a call never started (OpenAI refused the create)."""
    if hold <= 0:
        return
    await run_in_thread(
        metronome_fast_gate._table().update_item, Key={"owner_id": owner_id},
        UpdateExpression="SET openrouter_reserved_microcents = openrouter_reserved_microcents - :h",
        ConditionExpression="attribute_exists(owner_id) AND openrouter_reserved_microcents >= :h",
        ExpressionAttributeValues={":h": hold},
    )


async def _claim(call_id: str, now: int) -> dict | None:
    """open → closing, or re-claim a closing row whose closer died >5 min ago."""
    try:
        resp = await run_in_thread(
            _table().update_item, Key=_key(call_id),
            UpdateExpression="SET #s = :closing, closingAt = :now",
            ConditionExpression="#s = :open OR (#s = :closing AND closingAt < :stale)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":closing": CLOSING, ":open": OPEN, ":now": now, ":stale": now - 300},
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


async def _stop_turn(call: dict) -> None:
    """Defined in Task 5 (needs the runtime); a no-op here keeps this task self-contained."""


async def close_call(call_id: str, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    call = await _claim(call_id, now)
    if call is None:
        return False
    await _stop_turn(call)
    seconds = await openai_live.close_session(call["sessionId"])
    if seconds is None:
        # ponytail: fallback bills wall-clock to the deadline; the dev spike may switch this to 0.
        put_metric("voice.usage.fallback")
        seconds = max(0, min(now, int(call["deadline"])) - int(call["startedAt"]))
    await _bill(call, int(seconds))
    return True


async def _bill(call: dict, seconds: int) -> None:
    owner = call["ownerId"]
    call_key = {"owner_id": {"S": f"{CALL_KEY_PREFIX}{call['callId']}"}}
    delete = {"Delete": {"TableName": _table().name, "Key": call_key,
                         "ConditionExpression": "#s = :closing", "ExpressionAttributeNames": {"#s": "status"},
                         "ExpressionAttributeValues": {":closing": {"S": CLOSING}}}}
    if call["billing"] == "credits":
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
            await _metronome(http).ingest([{
                "transaction_id": call["callId"], "customer_id": owner, "event_type": "voice_call",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(call["startedAt"]))),
                "properties": {"provider_cost_nanodollars": str(voice_billing.provider_nanodollars(seconds)),
                               "seconds": str(seconds), "agent_id": call["agentId"]},
            }])
        first = {"Update": {
            "TableName": _fast_gate_table_name(), "Key": {"owner_id": {"S": owner}},
            "UpdateExpression": "SET openrouter_reserved_microcents = openrouter_reserved_microcents - :hold ADD remaining_microcents :debit",
            "ConditionExpression": "attribute_exists(owner_id) AND openrouter_reserved_microcents >= :hold",
            "ExpressionAttributeValues": {":debit": {"N": str(-voice_billing.charge_microcents(seconds))},
                                          ":hold": {"N": str(int(call["holdMicrocents"]))}},
        }}
    else:
        today = voice_billing.trial_cap.utc_today()
        first = {"Update": {
            "TableName": _table().name, "Key": {"owner_id": {"S": f"{voice_billing.FREE_KEY_PREFIX}{owner}"}},
            "UpdateExpression": "SET usedSeconds = if_not_exists(usedSeconds, :zero) + :s, #d = :day",
            "ConditionExpression": "attribute_not_exists(#d) OR #d = :day",
            "ExpressionAttributeNames": {"#d": "day"},
            "ExpressionAttributeValues": {":s": {"N": str(seconds)}, ":zero": {"N": "0"}, ":day": {"S": today}},
        }}
    try:
        await run_in_thread(_table().meta.client.transact_write_items, TransactItems=[first, delete])
    except ClientError as exc:
        if call["billing"] == "free" and exc.response["Error"]["Code"] == "TransactionCanceledException":
            # Counter still holds yesterday: reset it to this call's seconds.
            first["Update"]["UpdateExpression"] = "SET usedSeconds = :s, #d = :day"
            first["Update"]["ConditionExpression"] = "#d <> :day"
            first["Update"]["ExpressionAttributeValues"].pop(":zero")
            await run_in_thread(_table().meta.client.transact_write_items, TransactItems=[first, delete])
        else:
            raise
    put_metric("voice.call.closed", dimensions={"billing": call["billing"]})
```

Add one test for the day rollover: `transact_write_items.side_effect = [ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"), None]` → the second call's update expression is `"SET usedSeconds = :s, #d = :day"`.

- [ ] **Step 3: Commit**

```bash
git add apps/backend/core/services/voice_calls.py apps/backend/tests/unit/services/test_voice_calls_close.py
git commit -m "feat(voice): call store and the single close/bill seam

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Start / turn / end routes

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py` (start, turn, `_stop_turn`, prompts)
- Modify: `apps/backend/core/services/live_events.py` (extract `user_channel`)
- Create: `apps/backend/routers/calls.py`
- Modify: `apps/backend/main.py` (`include_router`)
- Test: `apps/backend/tests/unit/routers/test_calls.py`, `apps/backend/tests/unit/services/test_live_events.py` (add one test)

**Interfaces:**
- Consumes: Tasks 2–4; `PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)` (`.company_id`); `routers.messaging._verify_paperclip_hermes_identity(owner_id, identity, full=True)`; `container_repo.get_by_owner_id(owner_id)`; `runtime_transport.request`; `billing_repo.get_by_owner_id`; `metronome_fast_gate._table()`.
- Produces:
  - `LiveEventPublisher.user_channel(owner_id: str, user_id: str) -> str | None` (e.g. `/ws/<ws>/users/<key>`)
  - `POST /api/v1/calls` body `{agentId: str, agentName: str, sdp: str}` (the name is display-only, used in the persona) → `{callId, sdp, deadline}`; errors 402 `{code:"voice_limit", reason}`, 503 `{code:"voice_unavailable"}`, 502 `{code:"voice_upstream"}`, 409 `{code:"live_channel_missing"}`
  - `POST /api/v1/calls/{callId}/turns` body `{utterance: str, transcript: str}` → `{turnId}`
  - `POST /api/v1/calls/{callId}/end` → `{ended: true}`
  - bridge body sent to `POST /internal/calls/v1/runs`: `{"identity": {runId, companyId, agentId, callId, sessionId: "agent:<a>:lightbulb:call:<callId>"}, "request": {input, instructions, session_id, frames: {endpoint, channel}}}`

- [ ] **Step 1: `user_channel`** — in `live_events.py`, move the resolution half of `publish_user` into:

```python
    async def user_channel(self, owner_id: str, user_id: str) -> str | None:
        mapping = await messaging_repo.get_user_stream(owner_id, user_id)
        if mapping is None or mapping.get("active") is not True:
            return None
        key = _uuid(str(mapping.get("channelKey", "")), "user channel key")
        reverse = await messaging_repo.get_stream(key)
        if (reverse is None or reverse.get("active") is not True
                or reverse.get("ownerId") != owner_id or reverse.get("userId") != user_id):
            return None
        workspace = _uuid(await messaging_repo.workspace_id_for_meta(messaging_repo.stream_pk(key), "META", reverse), "stream workspace id")
        return f"/ws/{workspace}/users/{key}"

    async def publish_user(self, owner_id: str, user_id: str, event: dict[str, Any]) -> bool:
        validated = _validated_event(event, USER_LIVE_EVENT_TYPES)
        channel = await self.user_channel(owner_id, user_id)
        if channel is None:
            return False
        await self._publish(channel, validated)
        return True
```

Existing `publish_user` tests must stay green unchanged; add one test asserting `user_channel` returns `None` when `get_user_stream` returns `None`.

- [ ] **Step 2: Write the failing router tests** `tests/unit/routers/test_calls.py`:

```python
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import calls as mod  # noqa: E402

svc = mod.voice_calls


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_a", org_id="org_a", org_role="org:member")
    yield TestClient(app)
    app.dependency_overrides.clear()


def _start_patches(*, mode="free", budget=600, key="sk-test"):
    return [
        patch.object(svc.settings, "OPENAI_API_KEY", key),
        patch.object(svc.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "active", "plan": "base"})),
        patch.object(svc.voice_billing, "billing_mode", return_value=mode),
        patch.object(svc.voice_billing, "budget_seconds", AsyncMock(return_value=budget)),
        patch.object(svc, "_company_id", AsyncMock(return_value="co-1")),
        patch.object(svc, "_verify_agent", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.openai_live, "create_session", AsyncMock(return_value=("live_1", "answer\r\n"))),
        patch.object(svc, "put_call", AsyncMock()),
        patch.object(svc, "_prewake", MagicMock()),
    ]


def _with(patches):
    for p in patches:
        p.start()
    return patches


def _stop(patches):
    for p in patches:
        p.stop()


def test_start_returns_answer_and_deadline(client):
    ps = _with(_start_patches())
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "offer\r\n"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["sdp"] == "answer\r\n" and body["callId"]
        call = svc.put_call.await_args.args[0]
        assert call["deadline"] - call["startedAt"] == 600
        assert call["billing"] == "free" and call["status"] == "call_open"
    finally:
        _stop(ps)


def test_start_without_key_is_503(client):
    ps = _with(_start_patches(key=""))
    try:
        assert client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).json()["detail"]["code"] == "voice_unavailable"
    finally:
        _stop(ps)


def test_start_under_a_minute_is_402(client):
    ps = _with(_start_patches(budget=59))
    try:
        resp = client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"})
        assert resp.status_code == 402
        assert resp.json()["detail"] == {"code": "voice_limit", "reason": "free_minutes_used"}
    finally:
        _stop(ps)


def test_start_inactive_plan_is_402(client):
    ps = _with(_start_patches(mode=None))
    try:
        assert client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).json()["detail"]["reason"] == "plan_inactive"
    finally:
        _stop(ps)


def test_start_credits_holds_and_releases_on_upstream_failure(client):
    ps = _with(_start_patches(mode="credits", budget=300))
    hold = patch.object(svc, "_hold", AsyncMock(return_value=30_000)).start()
    release = patch.object(svc, "release_hold", AsyncMock()).start()
    svc.openai_live.create_session.side_effect = svc.openai_live.LiveUpstreamError("400")
    try:
        assert client.post("/api/v1/calls", json={"agentId": "agent-1", "agentName": "Finance", "sdp": "x"}).status_code == 502
        hold.assert_awaited_once_with("org_a", 300)
        release.assert_awaited_once_with("org_a", 30_000)
    finally:
        patch.stopall()


OPEN_CALL = {"callId": "call-1", "ownerId": "org_a", "userId": "user_a", "agentId": "agent-1", "companyId": "co-1",
             "status": "call_open", "turnRunId": None, "deadline": 4102444800}


def test_turn_starts_a_call_scoped_run(client):
    runtime = AsyncMock(return_value=MagicMock(status_code=202))
    with (
        patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))),
        patch.object(svc, "set_turn", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn"})),
        patch.object(svc.runtime_transport, "request", runtime),
    ):
        resp = client.post("/api/v1/calls/call-1/turns", json={"utterance": "check payouts", "transcript": "User: hi"})
    assert resp.status_code == 200
    body = runtime.await_args.kwargs["json_body"]
    assert runtime.await_args.args[1] == "/internal/calls/v1/runs"
    assert body["identity"]["sessionId"] == "agent:agent-1:lightbulb:call:call-1"
    assert body["identity"]["callId"] == "call-1"
    assert body["request"]["session_id"] == body["identity"]["sessionId"]
    assert body["request"]["frames"]["channel"] == "/ws/ws-1/users/key-1"
    assert "check payouts" in body["request"]["input"]


def test_turn_supersedes_previous_run(client):
    runtime = AsyncMock(return_value=MagicMock(status_code=202))
    with (
        patch.object(svc, "get_call", AsyncMock(return_value={**OPEN_CALL, "turnRunId": "old-run"})),
        patch.object(svc, "set_turn", AsyncMock()),
        patch.object(svc, "_frame_channel", AsyncMock(return_value="/ws/ws-1/users/key-1")),
        patch.object(svc.container_repo, "get_by_owner_id", AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn"})),
        patch.object(svc.runtime_transport, "request", runtime),
    ):
        client.post("/api/v1/calls/call-1/turns", json={"utterance": "actually stop", "transcript": ""})
    first = runtime.await_args_list[0]
    assert first.args[1] == "/internal/calls/v1/runs/old-run/stop"


def test_turn_rejects_other_user(client):
    with patch.object(svc, "get_call", AsyncMock(return_value={**OPEN_CALL, "userId": "user_b"})):
        assert client.post("/api/v1/calls/call-1/turns", json={"utterance": "x", "transcript": ""}).status_code == 404


def test_end_closes(client):
    with patch.object(svc, "get_call", AsyncMock(return_value=dict(OPEN_CALL))), patch.object(mod.voice_calls, "close_call", AsyncMock(return_value=True)) as close:
        assert client.post("/api/v1/calls/call-1/end").json() == {"ended": True}
    close.assert_awaited_once_with("call-1")
```

- [ ] **Step 3: Implement the service half** — append to `core/services/voice_calls.py` (and add the imports listed at the top of the block):

```python
import asyncio
import uuid

from fastapi import HTTPException

from core.repositories import billing_repo, container_repo
from core.repositories.paperclip_repo import PaperclipRepo  # match the import path routers/messaging.py uses
from core.services import runtime_transport
from core.services.live_events import get_live_event_publisher

_BRIDGE_RUNS = "/internal/calls/v1/runs"
_TURN_TIMEOUT_S = 25.0  # under API Gateway's 30 s; the call start pre-wakes the runtime
_TRANSCRIPT_CHARS = 6000
_background: set[asyncio.Task] = set()

PERSONA = """You are {agent}, speaking with a teammate on a live voice call. Speak naturally at an unhurried pace. Be clear and direct, not overly cheerful. If they are frustrated, acknowledge it briefly and focus on the next helpful step.

Backchannel policy: use moderate backchannels ("mm-hm", "got it") while they talk.
Interruption policy: stop speaking when they interrupt, and listen.

Delegation policy: you have one backend, {agent}'s full agent: it can run commands, browse the web, read and edit files, file and update issues, remember things across sessions, and reason carefully. It does the real work and knows the facts. Delegate for any fact, current information or reasoning, for any do/check/find/make/fix/run/remember request, and for corrections to work already requested. Do not delegate greetings, small talk, repeating a result already given, or brief clarifications. Delegate before giving an answer that depends on backend work. Never guess a result while waiting: say briefly that you are checking, then wait."""

TURN_NOTE = """[Note: this request comes from a live spoken conversation. The text is a voice transcript (it may contain mis-hearings, hesitations and later corrections; use the latest intent). Your reply will be spoken aloud by a voice model that paraphrases it: answer in plain conversational sentences, keep it short (a few sentences unless the user asked for detail), no markdown, no lists, no code blocks, no URLs read out character by character. Do the work with your tools as usual; only the final facts need to be spoken. Do not claim an action succeeded before it actually did.]"""


def _problem(status: int, **detail) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


async def _company_id(owner_id: str) -> str:
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    if row is None:
        raise _problem(409, code="workspace_not_ready")
    return row.company_id


async def _verify_agent(owner_id: str, company_id: str, agent_id: str) -> None:
    from routers.messaging import _verify_paperclip_hermes_identity  # the one agent-belongs-to-company check

    await _verify_paperclip_hermes_identity(owner_id, {"companyId": company_id, "agentId": agent_id}, full=True)


async def _frame_channel(owner_id: str, user_id: str) -> str:
    channel = await get_live_event_publisher().user_channel(owner_id, user_id)
    if channel is None:
        raise _problem(409, code="live_channel_missing")
    return channel


async def _hold(owner_id: str, seconds: int) -> int:
    # ponytail: read-then-hold is racy across two simultaneous starts; the gate's own reservation
    # and the next Metronome refresh bound the overspend to one call. Upgrade: conditional hold.
    hold = voice_billing.charge_microcents(seconds)
    await run_in_thread(
        metronome_fast_gate._table().update_item, Key={"owner_id": owner_id},
        UpdateExpression="SET openrouter_reserved_microcents = if_not_exists(openrouter_reserved_microcents, :z) + :h",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":h": hold, ":z": 0},
    )
    return hold


def _identity(call: dict, run_id: str) -> dict:
    # Must equal the bridge's turn.call_session_key: a call has its own Hermes session, not a room's
    # and not a Paperclip run's.
    return {
        "runId": run_id, "companyId": call["companyId"], "agentId": call["agentId"], "callId": call["callId"],
        "sessionId": f"agent:{call['agentId']}:lightbulb:call:{call['callId']}",
    }


async def _runtime(call: dict, path: str, body: dict, *, timeout_s: float = _TURN_TIMEOUT_S):
    container = await container_repo.get_by_owner_id(call["ownerId"])
    if not container:
        raise _problem(409, code="workspace_not_ready")
    return await runtime_transport.request(
        container, path, method="POST", json_body=body,
        headers={"Authorization": f"Bearer {container.get('gateway_token') or ''}"}, timeout_s=timeout_s,
    )


def _prewake(call: dict) -> None:
    """Stopping this call's (not-yet-existing) run wakes the microVM; the bridge's 404 is expected."""
    async def wake():
        try:
            identity = _identity(call, call["callId"])
            container = await container_repo.get_by_owner_id(call["ownerId"])
            if container:
                await runtime_transport.request(
                    container, f"{_BRIDGE_RUNS}/{call['callId']}/stop", method="POST", json_body={"identity": identity},
                    headers={"Authorization": f"Bearer {container.get('gateway_token') or ''}"},
                    timeout_s=runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S,
                )
        except Exception:  # noqa: BLE001 -- best effort; the first turn wakes it otherwise
            logger.info("voice prewake failed", exc_info=True)

    task = asyncio.create_task(wake())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def start_call(*, owner_id: str, user_id: str, agent_id: str, agent_name: str, sdp: str) -> dict:
    if not settings.OPENAI_API_KEY:
        raise _problem(503, code="voice_unavailable")
    mode = voice_billing.billing_mode(await billing_repo.get_by_owner_id(owner_id))
    if mode is None:
        raise _problem(402, code="voice_limit", reason="plan_inactive")
    budget = await voice_billing.budget_seconds(owner_id, mode)
    if budget < voice_billing.MIN_CALL_SECONDS:
        raise _problem(402, code="voice_limit", reason="free_minutes_used" if mode == "free" else "credits_low")
    company_id = await _company_id(owner_id)
    await _verify_agent(owner_id, company_id, agent_id)
    await _frame_channel(owner_id, user_id)  # fail before paying for a session the client can't hear results on
    hold = await _hold(owner_id, budget) if mode == "credits" else 0
    try:
        session_id, answer = await openai_live.create_session(sdp=sdp, instructions=PERSONA.format(agent=agent_name))
    except openai_live.LiveUpstreamError:
        await release_hold(owner_id, hold)
        raise _problem(502, code="voice_upstream")
    now = int(time.time())
    call = {"callId": str(uuid.uuid4()), "ownerId": owner_id, "userId": user_id, "agentId": agent_id,
            "companyId": company_id, "sessionId": session_id, "startedAt": now, "deadline": now + budget,
            "billing": mode, "holdMicrocents": hold, "status": OPEN, "turnRunId": None}
    await put_call(call)
    _prewake(call)
    return {"callId": call["callId"], "sdp": answer, "deadline": call["deadline"]}


async def _own_open_call(call_id: str, owner_id: str, user_id: str) -> dict:
    call = await get_call(call_id)
    if not call or call["ownerId"] != owner_id or call["userId"] != user_id or call["status"] != OPEN:
        raise _problem(404, code="call_not_found")
    return call


async def _stop_turn(call: dict) -> None:  # replaces the Task 4 no-op
    run_id = call.get("turnRunId")
    if not run_id:
        return
    try:
        await _runtime(call, f"{_BRIDGE_RUNS}/{run_id}/stop", {"identity": _identity(call, run_id)}, timeout_s=10.0)
    except Exception:  # noqa: BLE001 -- a turn we can't stop finishes and its frames are dropped client-side
        logger.info("voice turn stop failed", exc_info=True)


async def start_turn(*, call_id: str, owner_id: str, user_id: str, utterance: str, transcript: str) -> dict:
    call = await _own_open_call(call_id, owner_id, user_id)
    await _stop_turn(call)
    run_id = str(uuid.uuid4())
    identity = _identity(call, run_id)
    context = transcript[-_TRANSCRIPT_CHARS:].strip()
    text = (f"[Recent spoken conversation, newest last:\n{context}]\n\n" if context else "") + utterance
    body = {"identity": identity, "request": {
        "input": text, "instructions": TURN_NOTE, "session_id": identity["sessionId"],
        "frames": {"endpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT, "channel": await _frame_channel(owner_id, user_id)},
    }}
    try:
        resp = await _runtime(call, _BRIDGE_RUNS, body)
    except (httpx.HTTPError, ValueError):
        raise _problem(503, code="runtime_unavailable")
    if resp.status_code not in (200, 202):
        raise _problem(503, code="runtime_unavailable")
    await set_turn(call_id, run_id)
    return {"turnId": run_id}


async def end_call(*, call_id: str, owner_id: str, user_id: str) -> dict:
    await _own_open_call(call_id, owner_id, user_id)
    await close_call(call_id)
    return {"ended": True}
```

Remove the Task 4 placeholder `_stop_turn` (the real one above replaces it). Keep module imports sorted at the top.

- [ ] **Step 4: Router** `routers/calls.py`:

```python
"""Voice calls with one agent. See docs/superpowers/specs/2026-09-30-voice-calls-design.md."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.services import voice_calls

router = APIRouter()


class StartBody(BaseModel):
    agentId: str = Field(min_length=1, max_length=128)
    agentName: str = Field(min_length=1, max_length=80)
    sdp: str = Field(min_length=1, max_length=64_000)


class TurnBody(BaseModel):
    utterance: str = Field(min_length=1, max_length=4_000)
    transcript: str = Field(default="", max_length=12_000)


@router.post("")
async def start(body: StartBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.start_call(owner_id=resolve_owner_id(auth), user_id=auth.user_id, agent_id=body.agentId,
                                        agent_name=body.agentName, sdp=body.sdp)


@router.post("/{call_id}/turns")
async def turn(call_id: str, body: TurnBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.start_turn(call_id=call_id, owner_id=resolve_owner_id(auth), user_id=auth.user_id,
                                        utterance=body.utterance, transcript=body.transcript)


@router.post("/{call_id}/end")
async def end(call_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await voice_calls.end_call(call_id=call_id, owner_id=resolve_owner_id(auth), user_id=auth.user_id)
```

`main.py`, next to the other `include_router` lines:

```python
app.include_router(calls.router, prefix="/api/v1/calls", tags=["calls"])
```

(and add `calls` to the `from routers import …` list).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/voice_calls.py apps/backend/core/services/live_events.py apps/backend/routers/calls.py apps/backend/main.py apps/backend/tests/unit
git commit -m "feat(voice): start, turn and end call routes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Deadline sweeper

**Files:**
- Modify: `apps/backend/core/services/voice_calls.py`
- Modify: `apps/backend/main.py` (lifespan: create, register, stop, three cleanup lists)
- Test: `apps/backend/tests/unit/services/test_voice_calls_sweeper.py`

**Interfaces:**
- Consumes: `container_repo.get_by_status`, `run_ledger.admitted`, `close_call`.
- Produces: `async def sweep_calls_once(now: int | None = None) -> int`, `async def voice_call_sweeper(stop: asyncio.Event) -> None`.

- [ ] **Step 1: Failing tests**

```python
from unittest.mock import AsyncMock, patch

import pytest

from core.services import voice_calls as vc


@pytest.mark.asyncio
async def test_sweep_closes_only_past_deadline_and_stale_closing():
    rows = {
        "call_open": [{"callId": "late", "deadline": 100}, {"callId": "live", "deadline": 10_000}],
        "call_closing": [{"callId": "stuck", "deadline": 100, "closingAt": 1}, {"callId": "busy", "deadline": 100, "closingAt": 900}],
    }
    with patch.object(vc.container_repo, "get_by_status", AsyncMock(side_effect=lambda s: rows[s])), \
         patch.object(vc, "close_call", AsyncMock(return_value=True)) as close:
        assert await vc.sweep_calls_once(now=1000) == 2
    assert sorted(c.args[0] for c in close.await_args_list) == ["late", "stuck"]
```

- [ ] **Step 2: Implement** (append to `voice_calls.py`):

```python
_SWEEP_SECONDS = 60


async def sweep_calls_once(now: int | None = None) -> int:
    now = int(time.time()) if now is None else now
    due = [c for c in await container_repo.get_by_status(OPEN) if int(c["deadline"]) <= now]
    due += [c for c in await container_repo.get_by_status(CLOSING) if int(c.get("closingAt", 0)) < now - 300]
    closed = 0
    for call in due:
        try:
            closed += bool(await close_call(call["callId"], now=now))
        except Exception:  # noqa: BLE001 -- one bad row never blocks the rest
            logger.warning("voice call close failed call=%s", call.get("callId"), exc_info=True)
    return closed


async def voice_call_sweeper(stop: asyncio.Event) -> None:
    from core.services.run_ledger import admitted

    while not stop.is_set():
        try:
            if await admitted("voice_calls", interval_s=_SWEEP_SECONDS):
                await sweep_calls_once()
        except Exception:  # noqa: BLE001
            logger.warning("voice call sweep failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=_SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 3: Wire into `main.py` lifespan** exactly like `browser_autosave`:

```python
    from core.services.voice_calls import voice_call_sweeper
    voice_sweeper_stop = asyncio.Event()
    voice_sweeper_task = asyncio.create_task(voice_call_sweeper(voice_sweeper_stop))
```

Add `"voice_call_sweeper": voice_sweeper_task,` to the `system_health.BACKGROUND_TASKS.update({…})` dict; `voice_sweeper_stop.set()` next to `browser_autosave_stop.set()`; and `voice_sweeper_task` to all three task lists (the `cleanup` gather, the cancel loop, the final gather).

- [ ] **Step 4: Commit**

```bash
git add apps/backend/core/services/voice_calls.py apps/backend/main.py apps/backend/tests/unit/services/test_voice_calls_sweeper.py
git commit -m "feat(voice): one-minute deadline sweeper

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Bridge — call runs with the DM-turn environment, streaming to the caller

A call turn is **not** a Paperclip run. It reuses the bridge's run registry (so `abort_run`
and `attach_agent` work), but gets its own routes, its own session key
(`agent:{agentId}:lightbulb:call:{callId}`), no durable S3 record (a call leaves nothing
behind), and the **same environment a DM turn gets**: `_secret_scope(agent_id)` with no
Paperclip overlay, the turn-context file the lightbulb plugin reads, family `"call"`. The
cloud browser works (its acquire takes no family); Mac routes refuse `"call"`
(`MacRun.family: Literal["room","paperclip"]`) — Mac tools in calls are a follow-up.

**Files:**
- Modify: `apps/infra/hermes/bridge/server.py` (`_CALL_RUN_RE`, `_call_envelope`, `_handle_call_run`, dispatch in `handle_plugin`)
- Modify: `apps/infra/hermes/bridge/turn.py` (`FramePublisher._event`, `call_frame_event`, `CallFramePublisher`, `register_call_run`, `_run_call`, `run_call_turn`)
- Test: `apps/infra/hermes/tests/test_voice_call_run.py`

**Interfaces:**
- Consumes (from Task 5): `POST /internal/calls/v1/runs` body
  `{"identity": {runId, companyId, agentId, callId, sessionId}, "request": {input, instructions, session_id, frames: {endpoint, channel}}}`;
  `POST /internal/calls/v1/runs/{runId}/stop` body `{"identity": {…same…}}`.
- Produces: 202 `{"run_id", "status": "queued"}` / 200 replay / 400 `invalid_call_run` / 503 `recycling`; stop → 202 `{"status": "stopping"}` or 404 `run_not_found`.
  AppSync event `{"schemaVersion":1,"eventId","type":"call.turn.frame","occurredAt","payload":{"callId","turnId","agentId","frame":{…,"runId","agentId","seq"}}}`;
  frame types as room runs: `run_active`, `tool_start`, `tool_end`, `tool_error`, `agent_activity`, `chunk`, `thinking`, `done`, `error`, `run_ended` (`outcome`: `done` | `aborted` | `error`).

- [ ] **Step 1: Failing tests** `tests/test_voice_call_run.py`:

```python
from types import SimpleNamespace

from bridge import server, turn

AUTH = {"authorization": "Bearer gw-test"}
SESSION = "agent:agent-1:lightbulb:call:call-1"
IDENTITY = {"runId": "run-1", "companyId": "company-1", "agentId": "agent-1", "callId": "call-1", "sessionId": SESSION}
REQUEST = {"input": "check payouts", "instructions": "speak briefly", "session_id": SESSION,
           "frames": {"endpoint": "https://e.invalid", "channel": "/ws/w/users/k"}}
BODY = {"identity": IDENTITY, "request": REQUEST}


def test_envelope_accepts_a_call():
    assert server._call_envelope(BODY, require_request=True) is not None


def test_envelope_rejects_a_session_key_for_another_call():
    assert server._call_envelope({**BODY, "identity": {**IDENTITY, "callId": "call-2"}}, require_request=True) is None


def test_envelope_rejects_a_paperclip_session_key():
    pc = "paperclip:company:company-1:agent:agent-1:run:run-1"
    assert server._call_envelope({"identity": {**IDENTITY, "sessionId": pc}, "request": {**REQUEST, "session_id": pc}}, require_request=True) is None


def test_create_registers_and_spawns(monkeypatch):
    spawned = []
    monkeypatch.setattr(server.threading, "Thread", lambda target, args, **_k: SimpleNamespace(start=lambda: spawned.append((target, args))))
    status, body = server.handle_plugin("/internal/calls/v1/runs", "POST", AUTH, BODY)
    assert (status, body["status"]) == (202, "queued")
    assert spawned[0][0] is turn.run_call_turn
    assert turn.RUNS["run-1"]["kind"] == "call"


def test_stop_aborts_a_live_call_run(monkeypatch):
    turn.register_call_run(IDENTITY)
    status, body = server.handle_plugin("/internal/calls/v1/runs/run-1/stop", "POST", AUTH, {"identity": IDENTITY})
    assert (status, body["status"]) == (202, "stopping")
    assert turn.RUNS["run-1"]["aborted"] is True


def test_stop_unknown_run_is_404():
    status, _ = server.handle_plugin("/internal/calls/v1/runs/nope/stop", "POST", AUTH, {"identity": {**IDENTITY, "runId": "nope"}})
    assert status == 404


def test_call_frames_use_the_call_envelope():
    calls = []
    pub = turn.CallFramePublisher("https://e.invalid", "/ws/w/users/k", "call-1", "agent-1", "run-1",
                                  publish=lambda _e, channel, *events: calls.append((channel, events)) or 200)
    pub.tool_start("tc-1", "browser", {})
    channel, events = calls[-1]
    assert channel == "/ws/w/users/k"
    assert events[0]["type"] == "call.turn.frame"
    assert events[0]["payload"]["callId"] == "call-1" and events[0]["payload"]["turnId"] == "run-1"
    assert events[0]["payload"]["frame"]["type"] == "tool_start"
    assert "roomId" not in events[0]


def test_call_turn_gets_the_dm_environment(monkeypatch):
    seen = {}
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _a: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _a: None)
    real_scope = turn._secret_scope

    def secret_scope(agent_id, overlay=None):
        seen["overlay"] = overlay
        return real_scope(agent_id, overlay)

    def begin_turn(run_id, agent_id, sink=None, family="room", room_id=None):
        seen["family"] = family
        return {}

    def construct(agent_id, body, frames, session_key):
        seen["frames"], seen["session"] = frames, session_key
        return SimpleNamespace(run_conversation=lambda **_kw: {"api_calls": 1, "final_response": "It landed."})

    monkeypatch.setattr(turn, "_secret_scope", secret_scope)
    monkeypatch.setattr(turn.browser_control, "begin_turn", begin_turn)
    monkeypatch.setattr(turn.browser_control, "release", lambda _l: None)
    monkeypatch.setattr(turn, "_construct_agent", construct)
    turn.register_call_run(IDENTITY)
    frames = turn.CallFramePublisher("", "", "call-1", "agent-1", "run-1")
    assert turn._run_call(IDENTITY, REQUEST, frames)[0::2] == ("completed", "It landed.")
    assert seen["overlay"] is None  # no PAPERCLIP_* variables
    assert seen["family"] == "call"
    assert seen["session"] == SESSION and seen["frames"] is frames
```

- [ ] **Step 2: turn.py — frames.** In `FramePublisher`, route event building through one hook:

```python
    def _event(self, frame: dict) -> dict:
        return frame_event(self._room_id, self._agent_id, self._run_id, self._thread_root, frame)

    def _send(self, frames: list[dict]) -> None:
        """Build the events and POST them in ONE guarded step — a frame never fails a run."""
        try:
            self._publish(self._endpoint, self._channel, *(self._event(f) for f in frames))
        except Exception:  # noqa: BLE001
            log.warning("frame emit failed", exc_info=True)
```

Below `frame_event`:

```python
def call_frame_event(call_id: str, turn_id: str, agent_id: str, frame: dict) -> dict:
    """A voice-call turn's frame, published to the CALLER's private user channel (no room)."""
    return {"schemaVersion": 1, "eventId": str(_uuid.uuid4()), "type": "call.turn.frame", "occurredAt": now(),
            "payload": {"callId": call_id, "turnId": turn_id, "agentId": agent_id,
                        "frame": {**frame, "runId": turn_id, "agentId": agent_id}}}
```

After the `FramePublisher` class:

```python
class CallFramePublisher(FramePublisher):
    def __init__(self, endpoint, channel, call_id, agent_id, run_id, publish=None):
        super().__init__(endpoint, channel, None, agent_id, run_id, None, publish)
        self._call_id = call_id

    def _event(self, frame: dict) -> dict:
        return call_frame_event(self._call_id, self._run_id, self._agent_id, frame)
```

- [ ] **Step 3: turn.py — the call run.** Next to `register_paperclip_run`:

```python
def call_session_key(agent_id: str, call_id: str) -> str:
    return f"agent:{agent_id}:lightbulb:call:{call_id}"


def register_call_run(identity: dict) -> bool:
    """Register a call turn in the run registry (abort/attach use it). False = already registered
    (a retried POST). No durable record: a call leaves nothing behind."""
    with _RUNS_LOCK:
        if identity["runId"] in RUNS:
            return False
        RUNS[identity["runId"]] = {
            "state": "queued", "agent": None, "aborted": False, "outcome": None, "started": False,
            "kind": "call", "identity": dict(identity), "output": "", "error": None,
            "usage": dict(ZERO_USAGE), "acceptedAt": time.time(),
        }
    return True


def _end_call_run(run_id: str, outcome: str) -> None:
    with _RUNS_LOCK:
        entry = RUNS.get(run_id)
        if entry is not None:
            entry.update(state="ended", outcome=outcome, agent=None)
    prune_runs()


def _run_call(identity: dict, request: dict, frames: "CallFramePublisher") -> tuple[str, str | None, str, dict]:
    """One call turn with the environment a DM turn gets: the profile's own secrets (no
    Paperclip overlay), MCP, terminal, logins and the lightbulb plugin's turn context."""
    agent_id, run_id, session_key = identity["agentId"], identity["runId"], identity["sessionId"]
    try:
        import run_agent  # noqa: F401
    except ImportError:
        pass
    with sync.profile_lock(agent_id):
        sync.ensure_profile(agent_id)
        sync.normalize_profile_backends(agent_id)
    ctx = {"apiUrl": API, "ownerId": OWNER, "hookToken": HOOK_TOKEN, "agentId": agent_id, "runId": run_id,
           "sessionKey": session_key, "roomId": None, "family": "call"}
    browser_lease = browser_control.begin_turn(str(run_id), agent_id, None, "call")
    logins = website_logins.prompt_scope(agent_id, None, None, lambda: run_aborted(run_id))
    with _waiting_turn_lock(agent_id, session_key, run_id), _hermes_home_scope(agent_id), _brokered_mcp_scope(runtime_mcp_env(agent_id)), _secret_scope(agent_id), _terminal_scope(agent_id, session_key), _mcp_session(agent_id), logins, _turn_context_file(agent_id, ctx):
        try:
            agent = _construct_agent(agent_id, {"sessionKey": session_key}, frames, session_key)
            attach_agent(run_id, agent)
            if run_aborted(run_id):
                return "aborted", None, "", dict(ZERO_USAGE)
            try:
                result = agent.run_conversation(
                    user_message=request["input"],
                    system_message=with_profile_agents_md(agent_id, request["instructions"]),
                ) or {}
                _post_turn.agent = agent
            except Exception:
                if run_aborted(run_id):
                    return "aborted", None, "", _agent_usage(agent)
                raise
        finally:
            browser_control.release(browser_lease)
    usage = _agent_usage(agent)
    if run_aborted(run_id):
        return "aborted", None, "", usage
    reply = result.get("final_response") or ""
    if result.get("failed") is True:
        return "error", _failed_kind(agent, result), "", usage
    if not result.get("api_calls"):
        return "error", "model_unavailable", "", usage
    if not reply:
        return "error", "empty_response", "", usage
    return "completed", None, reply, usage


def run_call_turn(identity: dict, request: dict, reserved: bool = False) -> None:
    run_id, agent_id, session_key = identity["runId"], identity["agentId"], identity["sessionId"]
    if not reserved:
        _busy_add(1)
    started = time.time()
    spec = request.get("frames") or {}
    frames = CallFramePublisher(spec.get("endpoint", ""), spec.get("channel", ""), identity["callId"], agent_id, run_id)
    outcome, error, output, usage = "error", None, "", dict(ZERO_USAGE)
    try:
        frames.started()
        try:
            sync.restore_root_auth()
            restore_profile(agent_id)
            RESTORED.add(agent_id)
            outcome, error, output, usage = ("aborted", None, "", usage) if run_aborted(run_id) else _run_call(identity, request, frames)
        except Exception:  # noqa: BLE001 -- the class only, never prompt data
            outcome = "aborted" if run_aborted(run_id) else "error"
            log.error("call turn %s failed\n%s", run_id, traceback.format_exc())
        _end_call_run(run_id, outcome)
        frames.finished(outcome, output, error)
        report_usage(session_key, agent_id, run_id, outcome == "completed", int((time.time() - started) * 1000), usage)
        _await_post_turn_writes(_pop_post_turn_agent())
        try:
            save_profile(agent_id)
        except Exception:  # noqa: BLE001 -- best effort, same as room turns
            log.warning("save_profile failed for call turn", exc_info=True)
        try:
            sync.save_root_auth()
        except Exception:  # noqa: BLE001
            log.warning("save_root_auth failed for call turn", exc_info=True)
    finally:
        _busy_add(-1)
```

(`frames.finished` takes the room vocabulary `completed | aborted | error`, which `_run_call` already returns.)

- [ ] **Step 4: server.py — routes.**

```python
_CALL_RUN_RE = re.compile(r"^/internal/calls/v1/runs/([A-Za-z0-9_-]+)/stop$")


def _call_envelope(body, *, path_run_id: str | None = None, require_request: bool = False):
    if not isinstance(body, dict) or not isinstance(body.get("identity"), dict):
        return None
    identity = body["identity"]
    if any(not isinstance(identity.get(k), str) or not identity[k] for k in ("runId", "companyId", "agentId", "callId", "sessionId")):
        return None
    if path_run_id is not None and identity["runId"] != path_run_id:
        return None
    if identity["sessionId"] != turn.call_session_key(identity["agentId"], identity["callId"]):
        return None
    request = body.get("request")
    if require_request:
        if not isinstance(request, dict) or request.get("session_id") != identity["sessionId"]:
            return None
        if any(not isinstance(request.get(k), str) or not request[k] for k in ("input", "instructions")):
            return None
        frames = request.get("frames")
        if not isinstance(frames, dict) or not all(isinstance(frames.get(k), str) and frames[k] for k in ("endpoint", "channel")):
            return None
    return identity, request


def _handle_call_run(path: str, method: str, body) -> tuple[int, dict] | None:
    if path == "/internal/calls/v1/runs" and method == "POST":
        parsed = _call_envelope(body, require_request=True)
        if parsed is None:
            return 400, {"error": "invalid_call_run"}
        identity, request = parsed
        if not turn.reserve_turn():
            return 503, {"error": "recycling"}
        try:
            if not turn.register_call_run(identity):
                turn.release_turn()
                return 200, {"run_id": identity["runId"], "status": "replayed"}
            threading.Thread(target=turn.run_call_turn, args=(dict(identity), dict(request), True),
                             daemon=True, name=f"call-{identity['runId']}").start()
        except Exception:
            turn.discard_run(identity["runId"])
            turn.release_turn()
            raise
        return 202, {"run_id": identity["runId"], "status": "queued"}
    match = _CALL_RUN_RE.fullmatch(path)
    if not match or method != "POST":
        return None
    parsed = _call_envelope(body, path_run_id=match.group(1))
    if parsed is None:
        return 400, {"error": "invalid_call_identity"}
    if not turn.abort_run(match.group(1)):
        return 404, {"error": "run_not_found"}
    return 202, {"run_id": match.group(1), "status": "stopping"}
```

In `handle_plugin`, right before the paperclip dispatch:

```python
    call = _handle_call_run(path, method, body)
    if call is not None:
        return call
```

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/bridge apps/infra/hermes/tests/test_voice_call_run.py
git commit -m "feat(bridge): call runs with the DM-turn environment, frames to the caller

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Shared `call.turn.frame` type, Metronome product, stale docs

**Files:**
- Modify: `packages/shared/src/live/types.ts`
- Test: `packages/shared/src/live/callTurnFrame.test.ts`
- Modify: `apps/backend/scripts/setup_metronome_openrouter_pricing.py` (+ its test if one exists: `ls apps/backend/tests/unit/scripts | grep pricing`)
- Modify: `apps/backend/core/billing/metronome_fast_gate.py` (docstrings), `CLAUDE.md` (lines 1242, 1263, 1451)

**Interfaces:**
- Produces: `CallTurnFrame` TS type `{callId: string; turnId: string; agentId: string; frame: {type: string; [k: string]: unknown}}`; `LiveEventOf<"call.turn.frame">`.

- [ ] **Step 1: Failing test**

```ts
import { describe, expect, it } from "vitest";
import { parseLiveEvent } from "./types";

const base = { schemaVersion: 1, eventId: "e1", type: "call.turn.frame", occurredAt: "2026-09-30T00:00:00Z" };

describe("call.turn.frame", () => {
  it("parses a call frame", () => {
    const event = parseLiveEvent({ ...base, payload: { callId: "c", turnId: "t", agentId: "a", frame: { type: "tool_start", tool: "browser" } } });
    expect(event?.type).toBe("call.turn.frame");
  });
  it("rejects a frame without a type", () => {
    expect(parseLiveEvent({ ...base, payload: { callId: "c", turnId: "t", agentId: "a", frame: {} } })).toBeNull();
  });
});
```

(If `parseLiveEvent` takes a JSON string, pass `JSON.stringify(...)` — match `digestReady.test.ts`.)

- [ ] **Step 2: Types** — add `| "call.turn.frame"` to `LiveEventType`, `"call.turn.frame",` to `LIVE_EVENT_TYPES`, and:

```ts
export interface CallTurnFrame {
  callId: string;
  turnId: string;
  agentId: string;
  frame: { type: string; [key: string]: unknown };
}
```

`| LiveEventEnvelope<"call.turn.frame", CallTurnFrame>` in `LiveEvent`, and in the payload switch:

```ts
    case "call.turn.frame":
      return str(payload.callId) && str(payload.turnId) && str(payload.agentId) && isObj(payload.frame) && str(payload.frame.type);
```

Export `CallTurnFrame` from `packages/shared/src/index.ts` next to the other live types.

- [ ] **Step 3: Metronome product** — in the setup script, add:

```python
PRODUCTS = {
    "inference": {"event_type": "openrouter_inference", "metric_name": "OpenRouter provider cost (nanodollars)", "product_name": "Managed AI usage"},
    "voice": {"event_type": "voice_call", "metric_name": "Voice call provider cost (nanodollars)", "product_name": "Voice calls"},
}
```

Give `setup(...)` keyword params `event_type: str = EVENT_TYPE, metric_name: str = METRIC_NAME, product_name: str = PRODUCT_NAME` and replace every use of those three module constants inside `setup` and the find/create helpers it calls with the parameters (thread them as arguments). `main()` gains `--product {inference,voice}` (default `inference`) and passes `**PRODUCTS[args.product]`. Test: call `setup(fake_client, rate_card_id="rc", markup=Decimal("1.2"), starting_at="2026-10-01T00:00:00Z", apply=True, **PRODUCTS["voice"])` against the existing fake client pattern and assert the metric-create body has `"event_type_filter": {"in_values": ["voice_call"]}`.

- [ ] **Step 4: Stale docs** — `metronome_fast_gate.py`: module docstring "The backend never depletes — usage flows from the meter Lambda…" → "Two debiters besides the authoritative set: the openrouter-settlement Lambda (inference) and the backend's voice-call close (``voice_calls._bill``); both are provisional and overwritten by the next refresh."; `set_balance` docstring "This is the ONLY writer of ``remaining_microcents``" → "This is the only authoritative SET of ``remaining_microcents``; settlement and voice closes ADD provisional debits." CLAUDE.md 1242/1263/1451: replace "sole Metronome writer" with "Metronome writer for inference (voice calls ingest from the backend's `voice_calls` close)".

- [ ] **Step 5: Commit, push, open PR 1**

```bash
git add packages/shared apps/backend/scripts apps/backend/tests apps/backend/core/billing/metronome_fast_gate.py CLAUDE.md
git commit -m "feat(voice): call.turn.frame live type, voice Metronome product, billing docs

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push -u origin feat/voice-calls
gh pr create --repo TheLightbulbCompany/lightbulb --title "feat(voice): server side of agent voice calls (dark until OPENAI_API_KEY)" --body "Implements PR 1 of docs/superpowers/plans/2026-09-30-voice-calls.md …

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

Watch CI (`gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`), handle Codex's review on all four surfaces, merge only when CI is green and findings are handled (separate commands).

### Task 8b: Roll the bridge change onto dev runtimes

- [ ] `build-hermes-image.yml` pushes `h-<date>-<sha7>` after the merge; open a PR flipping `hermes-version.json` `dev.tag` to it; merge; then `cd apps/backend && uv run python scripts/roll_hermes_runtimes.py --env dev` (read its `--help` first). Verify with `aws bedrock-agentcore-control get-agent-runtime` that `containerUri` carries the new tag. `prod.tag` flips only with the prod promotion.
- [ ] Run the Metronome setup for voice in dev: `uv run python scripts/setup_metronome_openrouter_pricing.py --env dev --product voice` (dry run), then `--apply`. Prod waits for the owner's prod promotion.

---

## PR 2 — shared call session + web + desktop entitlements

### Task 9: `VoiceCallSession` in `@lightbulb/shared`

**Files:**
- Create: `packages/shared/src/voice/session.ts`, `packages/shared/src/voice/sentences.ts`
- Create: `packages/shared/src/voice/session.test.ts`, `packages/shared/src/voice/sentences.test.ts`
- Modify: `packages/shared/src/index.ts` (exports)

**Interfaces:**
- Consumes: `ApiMethods` (`post<T>(path, body)`), `LiveEvent`, `LiveEventOf<"call.turn.frame">`.
- Produces:

```ts
export interface VoiceChannel { send(data: string): void; onmessage: ((ev: { data: string }) => void) | null; }
export interface VoicePeer { offerSdp: string; channel: VoiceChannel; accept(answerSdp: string): Promise<void>; close(): void; }
export interface VoiceRtc { open(): Promise<VoicePeer>; }
export type VoiceCallState = "connecting" | "live" | "ending" | "ended" | "failed";
export interface VoiceCallView { state: VoiceCallState; deadline: number | null; working: string | null; error: string | null; }
export class VoiceCallSession {
  constructor(deps: { api: ApiMethods; rtc: VoiceRtc; onEvent: (h: (e: LiveEvent) => void) => () => void; agentName: string; now?: () => number; setTimer?: typeof setTimeout; clearTimer?: typeof clearTimeout });
  start(agentId: string): Promise<void>;
  hangUp(): Promise<void>;
  subscribe(listener: (view: VoiceCallView) => void): () => void;
}
export function takeSentences(text: string): { ready: string; rest: string };
```

- [ ] **Step 1: Sentence splitter test + impl**

```ts
// sentences.test.ts
import { describe, expect, it } from "vitest";
import { takeSentences } from "./sentences";

describe("takeSentences", () => {
  it("returns complete sentences and keeps the tail", () => {
    expect(takeSentences("It landed. Four thousand two")).toEqual({ ready: "It landed.", rest: " Four thousand two" });
  });
  it("keeps everything when no sentence has ended", () => {
    expect(takeSentences("Checking the")).toEqual({ ready: "", rest: "Checking the" });
  });
  it("does not split decimals", () => {
    expect(takeSentences("It is $4.21 today. Next")).toEqual({ ready: "It is $4.21 today.", rest: " Next" });
  });
});
```

```ts
// sentences.ts
/** Split off every completed sentence (terminator followed by whitespace). */
export function takeSentences(text: string): { ready: string; rest: string } {
  const match = /^[\s\S]*[.!?](?=\s)/.exec(text);
  if (!match) return { ready: "", rest: text };
  return { ready: match[0].trim(), rest: text.slice(match[0].length) };
}
```

- [ ] **Step 2: Session tests** (`session.test.ts`) — a fake channel records sends; a fake `onEvent` lets the test push live events:

```ts
import { describe, expect, it, vi } from "vitest";
import type { LiveEvent } from "../live/types";
import { VoiceCallSession, type VoiceChannel } from "./session";

function harness() {
  const sent: Array<Record<string, unknown>> = [];
  const channel: VoiceChannel = { send: (d) => sent.push(JSON.parse(d)), onmessage: null };
  const handlers = new Set<(e: LiveEvent) => void>();
  const api = {
    post: vi.fn(async (path: string) => {
      if (path === "/calls") return { callId: "call-1", sdp: "answer", deadline: 1_000_000 };
      if (path.endsWith("/turns")) return { turnId: `turn-${api.post.mock.calls.length}` };
      return { ended: true };
    }),
  };
  const session = new VoiceCallSession({
    api: api as never,
    rtc: { open: async () => ({ offerSdp: "offer", channel, accept: async () => {}, close: vi.fn() }) },
    onEvent: (h) => { handlers.add(h); return () => handlers.delete(h); },
    agentName: "Finance",
    now: () => 0,
    setTimer: (() => 0) as never,
    clearTimer: (() => {}) as never,
  });
  const oai = (event: Record<string, unknown>) => channel.onmessage?.({ data: JSON.stringify(event) });
  const frame = (turnId: string, f: Record<string, unknown>) =>
    handlers.forEach((h) => h({ schemaVersion: 1, eventId: "e", type: "call.turn.frame", occurredAt: "", payload: { callId: "call-1", turnId, agentId: "a", frame: f } } as never));
  return { session, api, sent, oai, frame };
}

describe("VoiceCallSession", () => {
  it("posts a turn with the latest utterance on delegation", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "check the stripe payout" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledWith("/calls/call-1/turns", expect.objectContaining({ utterance: "check the stripe payout" })));
  });

  it("maps tool progress to thinking and answers to commentary", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "payout?" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    const turnId = (await h.api.post.mock.results[1].value).turnId;
    h.frame(turnId, { type: "tool_start", tool: "browser" });
    h.frame(turnId, { type: "chunk", content: "It landed this morning. ", append: true });
    h.frame(turnId, { type: "chunk", content: "$4,210", append: true });
    h.frame(turnId, { type: "run_ended", outcome: "done" });
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.thinking.append", delegation_id: "item_1", content: "Finance is working: browser. Not done yet." }));
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", delegation_id: "item_1", content: "It landed this morning." }));
    expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", delegation_id: "item_1", content: "$4,210" }));
  });

  it("drops frames for a superseded turn", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.oai({ type: "session.input_transcript.delta", delta: "first" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(2));
    const oldTurn = (await h.api.post.mock.results[1].value).turnId;
    h.oai({ type: "session.input_transcript.delta", delta: " no, second" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_2", target: "client" } });
    await vi.waitFor(() => expect(h.api.post).toHaveBeenCalledTimes(3));
    h.frame(oldTurn, { type: "chunk", content: "Old answer. ", append: true });
    expect(h.sent.some((e) => e.content === "Old answer.")).toBe(false);
  });

  it("says it could not reach the agent when the turn fails", async () => {
    const h = harness();
    await h.session.start("agent-1");
    h.api.post.mockRejectedValueOnce(new Error("503"));
    h.oai({ type: "session.input_transcript.delta", delta: "hi" });
    h.oai({ type: "session.delegation.created", delegation: { id: "item_1", target: "client" } });
    await vi.waitFor(() => expect(h.sent).toContainEqual(expect.objectContaining({ type: "session.commentary.append", content: "Sorry, I couldn't reach Finance for that." })));
  });

  it("hangs up through the backend", async () => {
    const h = harness();
    await h.session.start("agent-1");
    await h.session.hangUp();
    expect(h.api.post).toHaveBeenLastCalledWith("/calls/call-1/end", {});
  });
});
```

- [ ] **Step 3: Implement `session.ts`**

```ts
import type { ApiMethods } from "../api/client";
import type { LiveEvent } from "../live/types";
import { takeSentences } from "./sentences";

export interface VoiceChannel { send(data: string): void; onmessage: ((ev: { data: string }) => void) | null; }
export interface VoicePeer { offerSdp: string; channel: VoiceChannel; accept(answerSdp: string): Promise<void>; close(): void; }
export interface VoiceRtc { open(): Promise<VoicePeer>; }
export type VoiceCallState = "connecting" | "live" | "ending" | "ended" | "failed";
export interface VoiceCallView { state: VoiceCallState; deadline: number | null; working: string | null; error: string | null; }

const TRANSCRIPT_CHARS = 6000;
const APPEND_CHARS = 1400; // OpenAI caps each append at 500 tokens
const CLOSE_WAIT_MS = 15_000;
const WARN_BEFORE_MS = 60_000;

interface Turn { turnId: string | null; delegationId: string; text: string; spoken: string; label: string | null; }

export class VoiceCallSession {
  private peer: VoicePeer | null = null;
  private callId: string | null = null;
  private lines: Array<{ role: "User" | "Voice assistant"; text: string }> = [];
  private turn: Turn | null = null;
  private listeners = new Set<(view: VoiceCallView) => void>();
  private view: VoiceCallView = { state: "connecting", deadline: null, working: null, error: null };
  private offEvents: (() => void) | null = null;
  private timers: unknown[] = [];

  constructor(private deps: {
    api: ApiMethods; rtc: VoiceRtc; onEvent: (h: (e: LiveEvent) => void) => () => void; agentName: string;
    now?: () => number; setTimer?: typeof setTimeout; clearTimer?: typeof clearTimeout;
  }) {}

  subscribe(listener: (view: VoiceCallView) => void): () => void {
    this.listeners.add(listener);
    listener(this.view);
    return () => this.listeners.delete(listener);
  }

  async start(agentId: string): Promise<void> {
    try {
      this.offEvents = this.deps.onEvent((e) => this.onLive(e));
      this.peer = await this.deps.rtc.open();
      this.peer.channel.onmessage = (ev) => this.onOai(JSON.parse(ev.data));
      const res = await this.deps.api.post<{ callId: string; sdp: string; deadline: number }>("/calls", { agentId, agentName: this.deps.agentName, sdp: this.peer.offerSdp });
      this.callId = res.callId;
      await this.peer.accept(res.sdp);
      this.set({ state: "live", deadline: res.deadline * 1000 });
      const warnIn = res.deadline * 1000 - WARN_BEFORE_MS - this.now();
      if (warnIn > 0) this.timers.push(this.timer(() => this.oai({ type: "session.instructions.append", content: "Tell the user there is about one minute left on this call." }), warnIn));
    } catch (error) {
      this.teardown();
      this.set({ state: "failed", error: errorCode(error) });
    }
  }

  async hangUp(): Promise<void> {
    if (!this.callId || this.view.state !== "live") return this.teardown();
    this.set({ state: "ending" });
    this.timers.push(this.timer(() => this.teardown(), CLOSE_WAIT_MS));
    try {
      await this.deps.api.post(`/calls/${this.callId}/end`, {});
    } finally {
      this.teardown();
    }
  }

  private onOai(event: { type?: string; delta?: string; delegation?: { id: string } }): void {
    if (event.type === "session.input_transcript.delta" && event.delta) this.note("User", event.delta);
    else if (event.type === "session.output_transcript.delta" && event.delta) this.note("Voice assistant", event.delta);
    else if (event.type === "session.delegation.created" && event.delegation) void this.delegate(event.delegation.id);
    else if (event.type === "session.closed") this.teardown();
  }

  private note(role: "User" | "Voice assistant", delta: string): void {
    const last = this.lines[this.lines.length - 1];
    if (last?.role === role) last.text += delta;
    else this.lines.push({ role, text: delta });
  }

  private async delegate(delegationId: string): Promise<void> {
    const utterance = [...this.lines].reverse().find((l) => l.role === "User")?.text.trim() ?? "";
    const transcript = this.lines.map((l) => `${l.role}: ${l.text.trim()}`).join("\n").slice(-TRANSCRIPT_CHARS);
    const turn: Turn = { turnId: null, delegationId, text: "", spoken: "", label: null };
    this.turn = turn; // any older turn's frames are now ignored
    try {
      const res = await this.deps.api.post<{ turnId: string }>(`/calls/${this.callId}/turns`, { utterance: utterance || "(no words heard)", transcript });
      turn.turnId = res.turnId;
    } catch {
      if (this.turn === turn) this.commentary(turn, `Sorry, I couldn't reach ${this.deps.agentName} for that.`);
    }
  }

  private onLive(event: LiveEvent): void {
    if (event.type !== "call.turn.frame") return;
    const { callId, turnId, frame } = event.payload;
    const turn = this.turn;
    if (callId !== this.callId || !turn || turn.turnId !== turnId) return;
    if (frame.type === "tool_start" && typeof frame.tool === "string" && frame.tool !== turn.label) {
      turn.label = frame.tool;
      this.set({ working: frame.tool });
      this.oai({ type: "session.thinking.append", delegation_id: turn.delegationId, content: `${this.deps.agentName} is working: ${frame.tool}. Not done yet.` });
    } else if (frame.type === "chunk" && typeof frame.content === "string") {
      turn.text = frame.append ? turn.text + frame.content : frame.content;
      this.speakReady(turn, false);
    } else if (frame.type === "error" && typeof frame.message === "string") {
      this.commentary(turn, `Sorry, I couldn't reach ${this.deps.agentName} for that.`);
    } else if (frame.type === "run_ended") {
      this.speakReady(turn, true);
      if (!turn.spoken) this.oai({ type: "session.thinking.append", delegation_id: turn.delegationId, content: `${this.deps.agentName} finished that request without a spoken result.` });
      this.set({ working: null });
    }
  }

  private speakReady(turn: Turn, final: boolean): void {
    const pending = turn.text.slice(turn.spoken.length);
    const { ready, rest } = final ? { ready: pending.trim(), rest: "" } : takeSentences(pending);
    if (!ready) return;
    turn.spoken = turn.text.slice(0, turn.text.length - rest.length);
    for (let i = 0; i < ready.length; i += APPEND_CHARS) this.commentary(turn, ready.slice(i, i + APPEND_CHARS));
  }

  private commentary(turn: Turn, content: string): void {
    this.oai({ type: "session.commentary.append", delegation_id: turn.delegationId, content });
  }

  private oai(event: Record<string, unknown>): void {
    this.peer?.channel.send(JSON.stringify({ event_id: `evt_${Math.random().toString(36).slice(2)}`, ...event }));
  }

  private teardown(): void {
    this.timers.forEach((t) => (this.deps.clearTimer ?? clearTimeout)(t as never));
    this.timers = [];
    this.offEvents?.();
    this.offEvents = null;
    this.peer?.close();
    this.peer = null;
    if (this.view.state !== "failed") this.set({ state: "ended", working: null });
  }

  private set(patch: Partial<VoiceCallView>): void {
    this.view = { ...this.view, ...patch };
    this.listeners.forEach((l) => l(this.view));
  }

  private now(): number { return (this.deps.now ?? Date.now)(); }
  private timer(fn: () => void, ms: number): unknown { return (this.deps.setTimer ?? setTimeout)(fn, ms); }
}

function errorCode(error: unknown): string {
  const detail = (error as { body?: { detail?: { code?: string; reason?: string } } })?.body?.detail;
  return detail?.reason ?? detail?.code ?? "call_failed";
}
```

(Check the shape `ApiMethods.post` throws on non-2xx in `src/api/client.ts` and adjust `errorCode` to read that error's body field — the test for 402 below pins it.)

Add to the session test file:

```ts
  it("reports the backend's limit reason when the call can't start", async () => {
    const h = harness();
    h.api.post.mockRejectedValueOnce(Object.assign(new Error("402"), { body: { detail: { code: "voice_limit", reason: "free_minutes_used" } } }));
    const views: string[] = [];
    h.session.subscribe((v) => views.push(`${v.state}:${v.error}`));
    await h.session.start("agent-1");
    expect(views.at(-1)).toBe("failed:free_minutes_used");
  });
```

Export from `packages/shared/src/index.ts`: `export * from "./voice/session"; export { takeSentences } from "./voice/sentences";`

- [ ] **Step 4: Commit**

```bash
git add packages/shared/src/voice packages/shared/src/index.ts
git commit -m "feat(voice): platform-neutral VoiceCallSession

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Web call button, in-call panel, CSP; desktop mic entitlement

**Files:**
- Create: `apps/frontend/src/lib/voiceRtc.ts`
- Create: `apps/frontend/src/components/messaging/VoiceCallButton.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomHeader.tsx`
- Modify: `apps/frontend/src/app/globals.css` (panel styles, tokens only)
- Modify: `apps/frontend/next.config.ts` (`connect-src`)
- Modify: `apps/desktop/src-tauri/entitlements.plist`, `apps/desktop/src-tauri/Info.plist`
- Test: `apps/frontend/src/components/messaging/__tests__/VoiceCallButton.test.tsx`

**Interfaces:**
- Consumes: `VoiceCallSession`, `VoiceRtc` (Task 9); `useApi()`; `useLiveEvents().onEvent`.
- Produces: `webVoiceRtc: VoiceRtc`; `<VoiceCallButton agentId agentName />`.

- [ ] **Step 1: Failing component test**

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const start = vi.fn(async () => {});
const hangUp = vi.fn(async () => {});
vi.mock("@lightbulb/shared", async (orig) => ({
  ...(await orig<object>()),
  VoiceCallSession: vi.fn().mockImplementation(() => ({ start, hangUp, subscribe: (l: (v: unknown) => void) => { l({ state: "live", deadline: Date.now() + 120_000, working: "browser", error: null }); return () => {}; } })),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: () => () => {} }) }));

import { VoiceCallButton } from "../VoiceCallButton";

describe("VoiceCallButton", () => {
  it("starts a call and shows progress and hang up", async () => {
    render(<VoiceCallButton agentId="agent-1" agentName="Finance" />);
    fireEvent.click(screen.getByRole("button", { name: "Call Finance" }));
    expect(start).toHaveBeenCalledWith("agent-1");
    expect(await screen.findByText("Finance is working: browser")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Hang up" }));
    expect(hangUp).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: `lib/voiceRtc.ts`**

```ts
import type { VoicePeer, VoiceRtc } from "@lightbulb/shared";

const ICE_WAIT_MS = 10_000;

export const webVoiceRtc: VoiceRtc = {
  async open(): Promise<VoicePeer> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const pc = new RTCPeerConnection();
    stream.getTracks().forEach((track) => pc.addTrack(track, stream));
    const audio = new Audio();
    audio.autoplay = true;
    pc.ontrack = (e) => { audio.srcObject = e.streams[0]; };
    const dc = pc.createDataChannel("oai-events"); // before the offer
    const channel = { send: (d: string) => dc.readyState === "open" && dc.send(d), onmessage: null as VoicePeer["channel"]["onmessage"] };
    dc.onmessage = (e) => channel.onmessage?.({ data: String(e.data) });
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise<void>((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const done = () => pc.iceGatheringState === "complete" && resolve();
      pc.addEventListener("icegatheringstatechange", done);
      setTimeout(resolve, ICE_WAIT_MS);
    });
    return {
      offerSdp: pc.localDescription?.sdp ?? "",
      channel,
      accept: (sdp) => pc.setRemoteDescription({ type: "answer", sdp }),
      close: () => { stream.getTracks().forEach((t) => t.stop()); dc.close(); pc.close(); audio.srcObject = null; },
    };
  },
};
```

- [ ] **Step 3: `VoiceCallButton.tsx`**

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { Phone, PhoneOff } from "lucide-react";
import { VoiceCallSession, type VoiceCallView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { webVoiceRtc } from "@/lib/voiceRtc";

const ERRORS: Record<string, string> = {
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
};

export function VoiceCallButton({ agentId, agentName }: { agentId: string; agentName: string }) {
  const api = useApi();
  const { onEvent } = useLiveEvents();
  const session = useRef<VoiceCallSession | null>(null);
  const [view, setView] = useState<VoiceCallView | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => () => void session.current?.hangUp(), []);
  useEffect(() => {
    if (view?.state !== "live") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [view?.state]);

  const start = () => {
    const s = new VoiceCallSession({ api, rtc: webVoiceRtc, onEvent, agentName });
    session.current = s;
    s.subscribe(setView);
    void s.start(agentId);
  };

  const active = view && (view.state === "connecting" || view.state === "live" || view.state === "ending");
  const left = view?.deadline ? Math.max(0, Math.round((view.deadline - now) / 1000)) : null;

  return (
    <>
      <Button type="button" variant="ghost" size="icon" aria-label={`Call ${agentName}`} disabled={!!active} onClick={start}>
        <Phone className="h-4 w-4" />
      </Button>
      {view && view.state !== "ended" && (
        <div className="voice-call-panel" role="dialog" aria-label={`Call with ${agentName}`}>
          <div className="voice-call-title">{agentName}</div>
          <div className="voice-call-status">
            {view.state === "connecting" && "Connecting…"}
            {view.state === "live" && (view.working ? `${agentName} is working: ${view.working}` : "On the call")}
            {view.state === "ending" && "Ending…"}
            {view.state === "failed" && (ERRORS[view.error ?? ""] ?? "The call couldn't start.")}
          </div>
          {left !== null && view.state === "live" && <div className="voice-call-left">{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</div>}
          {view.state === "failed" ? (
            <Button type="button" variant="ghost" size="sm" onClick={() => setView(null)}>Close</Button>
          ) : (
            <Button type="button" variant="ghost" size="sm" aria-label="Hang up" onClick={() => void session.current?.hangUp()}>
              <PhoneOff className="h-4 w-4" />
            </Button>
          )}
        </div>
      )}
    </>
  );
}
```

- [ ] **Step 4: Header + styles + CSP**
  - `RoomHeader.tsx`: inside `<div className="room-audience">`, after `{search}`:

```tsx
        {room?.kind === "dm" && counterparts.length === 1 && counterparts[0].kind === "agent" && (
          <VoiceCallButton agentId={counterparts[0].id} agentName={counterparts[0].name ?? "Agent"} />
        )}
```

(use whatever display-name field `RoomParticipant` carries — it's the same one `ParticipantAvatar` renders.)
  - `globals.css` (under the `[data-chat-theme]` rules):

```css
.voice-call-panel { position: fixed; right: 16px; bottom: 16px; z-index: 50; display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; border-radius: 12px; background: var(--plane-raised); color: var(--fg); border: 1px solid var(--plane-edge); }
.voice-call-title { font-weight: 600; }
.voice-call-status, .voice-call-left { color: var(--fg-soft); font-size: 13px; }
```

  - `next.config.ts` `connect-src`: append ` https://api.openai.com` to the template string.

- [ ] **Step 5: Desktop** — `entitlements.plist`, inside `<dict>`:

```xml
  <!-- Hardened runtime silently denies the mic without this (voice calls). -->
  <key>com.apple.security.device.audio-input</key><true/>
```

`Info.plist`:

```xml
  <key>NSMicrophoneUsageDescription</key>
  <string>Lightbulb uses the microphone for voice calls with your agents.</string>
```

- [ ] **Step 6: Commit, push, PR 2** (same CI/Codex/merge rules as PR 1). PR body notes: desktop verification happens on prod after promotion.

```bash
git add apps/frontend apps/desktop/src-tauri/entitlements.plist apps/desktop/src-tauri/Info.plist
git commit -m "feat(voice): web call button + panel, desktop mic entitlement

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## PR 3 — iPhone

### Task 11: `react-native-webrtc` build check (the spec's spike item 3)

- [ ] `cd apps/mobile && npx expo install react-native-webrtc @config-plugins/react-native-webrtc react-native-incall-manager`
- [ ] Add to `app.json` `plugins`: `["@config-plugins/react-native-webrtc", {"cameraPermission": false, "microphonePermission": "Lightbulb uses the microphone for voice calls with your agents."}]`. Leave `expo-image-picker`'s `"microphonePermission": false` (it only suppresses the picker's own string).
- [ ] `npx expo prebuild --platform ios --clean && npx expo run:ios` on the simulator. Pass = the app launches and `NativeModules.WebRTCModule` is defined. If the build fails on RN 0.86 / New Architecture, swap to `@stream-io/react-native-webrtc` (same API) and its plugin, and note it in the PR.
- [ ] Confirm the generated `ios/*/Info.plist` has exactly one `NSMicrophoneUsageDescription`.

### Task 12: Mobile call screen

**Files:**
- Create: `apps/mobile/src/lib/voiceRtc.ts`
- Create: `apps/mobile/src/app/(app)/call/[agentId].tsx`
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/index.tsx` (`RoomHeaderActions` gets `onCall`)
- Modify: the `RoomHeaderActions` component file (`grep -rn "function RoomHeaderActions" apps/mobile/src`)
- Test: `apps/mobile/src/lib/voiceRtc.test.ts`, extend `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx`

**Interfaces:**
- Consumes: `VoiceCallSession` (Task 9); mobile `useLiveEvents().onEvent`; the mobile API client (the same `ApiMethods` instance screens already use — `grep -rn "createApiClient" apps/mobile/src/lib`).
- Produces: `nativeVoiceRtc: VoiceRtc`, `hasNativeVoice(): boolean`; route `/(app)/call/[agentId]?name=`.

- [ ] **Step 1: Failing tests**

```ts
// voiceRtc.test.ts
import { NativeModules } from "react-native";
import { hasNativeVoice } from "./voiceRtc";

describe("hasNativeVoice", () => {
  it("is false on a binary without the WebRTC module (old build + new OTA)", () => {
    delete (NativeModules as Record<string, unknown>).WebRTCModule;
    expect(hasNativeVoice()).toBe(false);
  });
  it("is true when the module is linked", () => {
    (NativeModules as Record<string, unknown>).WebRTCModule = {};
    expect(hasNativeVoice()).toBe(true);
  });
});
```

In `conversation.test.tsx`, add: rendering an agent DM's `headerRight()` shows a button labelled `Call <agent name>` when `hasNativeVoice` is mocked `true`, and none when `false`.

- [ ] **Step 2: `lib/voiceRtc.ts`**

```ts
import { NativeModules } from "react-native";
import type { VoicePeer, VoiceRtc } from "@lightbulb/shared";

export function hasNativeVoice(): boolean {
  return !!(NativeModules as Record<string, unknown>).WebRTCModule;
}

export const nativeVoiceRtc: VoiceRtc = {
  async open(): Promise<VoicePeer> {
    // Required lazily: an old binary without the module must never evaluate these imports.
    const { RTCPeerConnection, mediaDevices } = require("react-native-webrtc");
    const InCallManager = require("react-native-incall-manager").default;
    const stream = await mediaDevices.getUserMedia({ audio: true });
    const pc = new RTCPeerConnection();
    stream.getTracks().forEach((t: unknown) => pc.addTrack(t, stream));
    const dc = pc.createDataChannel("oai-events");
    const channel = { send: (d: string) => dc.readyState === "open" && dc.send(d), onmessage: null as VoicePeer["channel"]["onmessage"] };
    dc.addEventListener("message", (e: { data: unknown }) => channel.onmessage?.({ data: String(e.data) }));
    InCallManager.start({ media: "audio" });
    InCallManager.setForceSpeakerphoneOn(true); // voice-chat mode defaults to the earpiece
    await pc.setLocalDescription(await pc.createOffer({}));
    await new Promise<void>((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      pc.addEventListener("icegatheringstatechange", () => pc.iceGatheringState === "complete" && resolve());
      setTimeout(resolve, 10_000);
    });
    return {
      offerSdp: pc.localDescription?.sdp ?? "",
      channel,
      accept: (sdp) => pc.setRemoteDescription({ type: "answer", sdp }),
      close: () => { stream.getTracks().forEach((t: { stop(): void }) => t.stop()); dc.close(); pc.close(); InCallManager.stop(); },
    };
  },
};
```

- [ ] **Step 3: Call screen** `app/(app)/call/[agentId].tsx` — mirrors the web panel: creates a `VoiceCallSession({api, rtc: nativeVoiceRtc, onEvent, agentName})` on mount and `start(agentId)`s it, renders agent name, status line (same copy and `ERRORS` map as web, colours from `chatPalette`), time left, and a Hang up button that awaits `hangUp()` then `router.back()`; `useEffect` cleanup calls `hangUp()`. Present it as a modal (`<Stack.Screen options={{ presentation: "modal", title: agentName }} />`).

- [ ] **Step 4: Header button** — `RoomHeaderActions` gains an optional `onCall?: () => void` rendering an SF Symbol `phone` button with `accessibilityLabel={`Call ${agentName}`}` when provided. In the room screen, pass `onCall` only when `room.kind === "dm"`, the counterpart is an agent, and `hasNativeVoice()`; it pushes `/(app)/call/${agentId}?name=${encodeURIComponent(agentName)}`.

- [ ] **Step 5: Commit, push, PR 3** — the PR merges the JS (OTA on old binaries hides the button); the feature reaches phones via a `mobile-v*` tag → `mobile-release.yml` → TestFlight.

```bash
git add apps/mobile
git commit -m "feat(voice): iPhone call screen over react-native-webrtc

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Acceptance (after the owner sets `lightbulb/dev/openai_api_key`)

- [ ] Force a new backend task (the next dev deploy) so ECS injects the key.
- [ ] Spike items 1–2 on dev, via Chrome (claude-in-chrome) on `https://dev.flickmeon.ai`: start a call to an agent, ask it a tool question ("what's on my calendar today"), hear progress + answer; hang up; confirm in CloudWatch that `voice.call.closed` fired and **not** `voice.usage.fallback`. Then close the tab mid-call and wait for the sweeper at the deadline (use a free workspace with little allowance left) — record whether the sideband attach on a dead session returned seconds or fell back; if it fell back, decide with the owner between the current wall-clock fallback and billing 0, and edit `close_call`.
- [ ] Credits workspace on dev: check the Metronome sandbox shows a `voice_call` event with the right `provider_cost_nanodollars`, and the fast-gate hold returned to its pre-call value.
- [ ] iPhone: TestFlight build, same call flow, speaker audio.
- [ ] Desktop: on prod after promotion, as the founder.
- [ ] Receipts (screenshots, metric names, event ids) on the PRs; then delete this plan and the spec in a follow-up PR (CLAUDE.md "Planning docs are disposable").
