# Provisioned Agent Email — Plan 2: Inbound

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An owner-verified sender emails their agent's `{slug}@{domain}` address; Resend receives it and webhooks us; we verify, resolve the recipient to `(owner, agent)`, gate on the sender allowlist + spend, and deliver the message into the owner's running container via OpenClaw's native `/hooks/agent`. Reply is out of scope (Plan 3).

**Architecture:** A new public webhook router (`routers/agent_mail.py`) mirrors the Clerk webhook (raw body → Svix verify → dedup → fast 200). It orchestrates three new services: `agent_mail_token` (a Fernet reply-context codec shared with outbound), `agent_mail_inbound` (parse sender, fetch the body from Resend's receiving REST API, format the delivered message), and `agent_mail_delivery` (resolve the running container and POST to `/hooks/agent`, mirroring `hooks_proxy.py`). Everything is inert unless BOTH `RESEND_WEBHOOK_SECRET` and `AGENT_EMAIL_DOMAIN` are set.

**Tech Stack:** Python 3 / FastAPI (uv, pytest, pytest-asyncio, moto, respx/httpx mock), Fernet (`cryptography`), Resend receiving REST API.

**Design spec:** `docs/superpowers/specs/2026-07-19-provisioned-agent-email-design.md` §6.4–§6.5.

## Global Constraints

- **Inert unless configured.** The webhook processes an event only when `settings.RESEND_WEBHOOK_SECRET` is a real `whsec_…` secret AND `settings.AGENT_EMAIL_DOMAIN` is non-empty. Otherwise it returns `200 {"status":"disabled"}` and does nothing — never a 5xx (a 5xx triggers Svix retry storms; see `routers/webhooks.py` docstring).
- **Fail-closed on signature.** Unlike Clerk (which skips verification when its secret is unset for local dev), an inbound email triggers billable agent runs — so when the secret IS set, a bad/missing signature is rejected (`401`); when the secret is UNSET the whole feature is disabled (above), never "process unsigned."
- **Sender is the security boundary.** Deliver only if the parsed `from` address is in the row's `verified_senders` (case-insensitive), unless `row["public"]` is true (off in v1). Otherwise drop with a metric — no bounce in v1.
- **Spend gate = trial cap, not paid-only.** Gate with `trial_cap.trial_block_reason(owner_id)`; over cap → drop. Do NOT require `has_always_on_entitlement` — a free container that is *running* may receive (the conversion hook). A container that is *not running* → silent drop (no wake; Resend retains the mail).
- **No caller-supplied sessionKey.** Containers set `allowRequestSessionKey: false`, so `/hooks/agent` is called WITHOUT a `sessionKey` (it runs an isolated turn). Do not send one.
- **Loop guard.** Drop if the fetched headers carry `Auto-Submitted` other than `no`, or `Precedence` in {`bulk`,`list`,`junk`,`auto_reply`} — an autoresponder must not ping-pong the agent into unbounded spend.
- **Dedup on `svix-id`** via the existing `webhook_dedup.record_event_or_skip`.
- **Body is fetched, not received.** The `email.received` webhook is metadata-only. Fetch `text`/`html`/`headers` from `GET https://api.resend.com/emails/receiving/{email_id}` with `Authorization: Bearer {RESEND_API_KEY}`. Use `httpx` directly (SDK-independent).
- Backend tests: `cd apps/backend && uv run pytest ... --no-cov -p no:cacheprovider`, FOREGROUND. Full CI scope is `pytest tests/` (NOT just `tests/unit`) — the heavier moto suite lives outside `tests/unit`.
- One pre-existing unrelated failure to ignore: `tests/unit/routers/teams/test_agents.py::test_create_agent_returns_upstream_status_on_paperclip_error`.
- Commit after each task.

---

### Task 1: config + reply-context codec

**Files:**
- Modify: `apps/backend/core/config.py` (add `RESEND_WEBHOOK_SECRET` in the Resend block, ~line 326)
- Create: `apps/backend/core/services/agent_mail_token.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_token.py`

**Interfaces:**
- Consumes: `core.encryption` Fernet helpers (grep `apps/backend/core/encryption.py` for the exact `encrypt`/`decrypt` function names and use them — do NOT hand-roll Fernet).
- Produces:
  - `ReplyContext` (a `TypedDict` or dataclass: `owner_id: str, agent_id: str, reply_to: str, in_reply_to: str, subject: str`)
  - `encode_reply_token(ctx: ReplyContext) -> str` — opaque URL-safe token
  - `decode_reply_token(token: str) -> ReplyContext | None` — returns None on any tamper/format/decrypt error (never raises)

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_agent_mail_token.py`:

```python
"""agent_mail_token: tamper-proof reply-context round-trip."""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "0" * 44)  # Fernet: 32 url-safe-base64 bytes

from core.services import agent_mail_token  # noqa: E402


def _ctx():
    return {
        "owner_id": "org_1",
        "agent_id": "agent_friday",
        "reply_to": "alice@example.com",
        "in_reply_to": "<msg-123@mail.example.com>",
        "subject": "Re: hello",
    }


def test_round_trip():
    token = agent_mail_token.encode_reply_token(_ctx())
    assert isinstance(token, str) and token
    assert agent_mail_token.decode_reply_token(token) == _ctx()


def test_token_is_opaque():
    token = agent_mail_token.encode_reply_token(_ctx())
    # No plaintext address leaks into the token.
    assert "alice@example.com" not in token


def test_decode_rejects_tampered_token():
    token = agent_mail_token.encode_reply_token(_ctx())
    assert agent_mail_token.decode_reply_token(token[:-4] + "AAAA") is None


def test_decode_rejects_garbage():
    assert agent_mail_token.decode_reply_token("not-a-token") is None
    assert agent_mail_token.decode_reply_token("") is None
```

- [ ] **Step 2: Run — expect FAIL** (`ImportError`).

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_token.py -v --no-cov -p no:cacheprovider`

- [ ] **Step 3: Implement.** First `grep -n "def encrypt\|def decrypt" apps/backend/core/encryption.py` to get the real names, then create `apps/backend/core/services/agent_mail_token.py`:

```python
"""Opaque, tamper-proof reply context for inbound email.

`/hooks/agent` has no metadata field, so the reply binding (who to answer,
which message to thread under) is inlined into the delivered message as an
opaque token. Encrypting it (Fernet) means the agent — and anyone who can
read the container's conversation — cannot forge a reply to an arbitrary
recipient: the outbound relay only honours a token IT can decrypt. Shared
with the outbound relay (Plan 3), which is the sole decoder.
"""

import json
import logging
from typing import TypedDict

from core.encryption import decrypt, encrypt  # use the real names from core/encryption.py

logger = logging.getLogger(__name__)


class ReplyContext(TypedDict):
    owner_id: str
    agent_id: str
    reply_to: str
    in_reply_to: str
    subject: str


def encode_reply_token(ctx: ReplyContext) -> str:
    return encrypt(json.dumps(ctx, separators=(",", ":")))


def decode_reply_token(token: str) -> ReplyContext | None:
    if not token:
        return None
    try:
        return json.loads(decrypt(token))
    except Exception:  # noqa: BLE001 — any tamper/format error → not a valid token
        return None
```

If `core/encryption.py`'s functions have different names/signatures (e.g. return bytes), adapt this file to them — the test pins the observable behavior, not the internals.

- [ ] **Step 4: Add the config field.** In `core/config.py`, in the Resend block beside `RESEND_API_KEY`:

```python
    # Inbound-email webhook signing secret (Svix `whsec_…`). Empty disables
    # the inbound webhook entirely (fail-closed — never process unsigned mail).
    RESEND_WEBHOOK_SECRET: str = os.getenv("RESEND_WEBHOOK_SECRET", "")
```

- [ ] **Step 5: Run — expect PASS.** Same command as Step 2.

- [ ] **Step 6: Commit.**

```bash
git add apps/backend/core/services/agent_mail_token.py apps/backend/tests/unit/services/test_agent_mail_token.py apps/backend/core/config.py
git commit -m "feat(agent-email): reply-context codec + inbound webhook secret config"
```

---

### Task 2: shared Svix verifier

**Files:**
- Create: `apps/backend/core/crypto/svix.py`
- Test: `apps/backend/tests/unit/crypto/test_svix.py`

**Interfaces:**
- Produces: `verify_svix_signature(*, secret: str, body: bytes, headers: dict[str, str]) -> bool` — True iff the `svix-id`/`svix-timestamp`/`svix-signature` headers validate the body under `secret`. Returns False on any missing header or mismatch; never raises.

**Why a new module:** `routers/webhooks.py:57-99` already implements this exact scheme inline for Clerk. Extract the pure logic so the new inbound router reuses it verbatim rather than a second hand-rolled copy. **Do NOT modify the Clerk router in this task** (it works and is on a critical path); a later cleanup can point it here.

- [ ] **Step 1: Write the failing test.** First read `routers/webhooks.py:57-99` to copy the exact algorithm (secret `removeprefix("whsec_")` → `base64.b64decode` → `signed = f"{id}.{ts}.".encode() + body` → `hmac.sha256` → base64 → compare against each space-separated `v1,<b64>` token with `hmac.compare_digest`).

Create `apps/backend/tests/unit/crypto/test_svix.py`:

```python
"""verify_svix_signature: HMAC-SHA256 over id.timestamp.body, Svix format."""

import base64
import hashlib
import hmac
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.crypto.svix import verify_svix_signature  # noqa: E402

_SECRET = "whsec_" + base64.b64encode(b"0" * 24).decode()


def _sign(body: bytes, msg_id: str, ts: str) -> str:
    key = base64.b64decode(_SECRET.removeprefix("whsec_"))
    signed = f"{msg_id}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return f"v1,{sig}"


def _headers(body: bytes, msg_id="msg_1", ts="1700000000"):
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": _sign(body, msg_id, ts),
    }


def test_valid_signature():
    body = b'{"hello":"world"}'
    assert verify_svix_signature(secret=_SECRET, body=body, headers=_headers(body)) is True


def test_wrong_body_fails():
    body = b'{"hello":"world"}'
    assert verify_svix_signature(secret=_SECRET, body=b'{"tampered":true}', headers=_headers(body)) is False


def test_missing_headers_fail():
    assert verify_svix_signature(secret=_SECRET, body=b"x", headers={}) is False


def test_multiple_signature_tokens_one_valid():
    body = b"x"
    good = _sign(body, "msg_1", "1700000000")
    hdrs = _headers(body)
    hdrs["svix-signature"] = f"v1,badbadbad {good}"  # space-separated list; one valid
    assert verify_svix_signature(secret=_SECRET, body=body, headers=hdrs) is True
```

- [ ] **Step 2: Run — expect FAIL.**

Run: `cd apps/backend && uv run pytest tests/unit/crypto/test_svix.py -v --no-cov -p no:cacheprovider`

(Create `apps/backend/tests/unit/crypto/__init__.py` if the dir doesn't exist.)

- [ ] **Step 3: Implement `apps/backend/core/crypto/svix.py`** by lifting the algorithm from `routers/webhooks.py:57-99` into a pure function with the signature above. Header lookups must be case-insensitive-tolerant (accept a plain dict; the caller passes `dict(request.headers)` which is already lowercased by Starlette).

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git add apps/backend/core/crypto/svix.py apps/backend/tests/unit/crypto/
git commit -m "feat(agent-email): extract reusable Svix signature verifier"
```

---

### Task 3: inbound parsing + body fetch + message formatting

**Files:**
- Create: `apps/backend/core/services/agent_mail_inbound.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_inbound.py`

**Interfaces:**
- Consumes: `agent_mail_token.encode_reply_token` (Task 1); `httpx`; `settings.RESEND_API_KEY`.
- Produces:
  - `parse_from_address(raw_from: str) -> str` — `"Acme <a@b.com>"` → `"a@b.com"`; bare `"a@b.com"` → `"a@b.com"`; lowercased/trimmed.
  - `is_auto_submitted(headers: dict[str, str]) -> bool` — loop guard on `Auto-Submitted` / `Precedence`.
  - `async fetch_received_email(email_id: str) -> dict | None` — GETs `https://api.resend.com/emails/receiving/{email_id}` with the API key; returns the parsed JSON (`text`, `html`, `headers`, `message_id`, …) or None on non-200 / error. Never raises.
  - `build_delivered_message(*, from_addr: str, subject: str, body_text: str, reply_token: str) -> str` — the string handed to `/hooks/agent`.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_agent_mail_inbound.py`:

```python
"""agent_mail_inbound: sender parsing, loop guard, body fetch, message format."""

import os

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_inbound as inbound  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Acme <onboarding@resend.dev>", "onboarding@resend.dev"),
        ("plain@example.com", "plain@example.com"),
        ("  Mixed <A@B.com> ", "a@b.com"),
        ('"Doe, Jane" <jane@x.io>', "jane@x.io"),
    ],
)
def test_parse_from_address(raw, expected):
    assert inbound.parse_from_address(raw) == expected


@pytest.mark.parametrize(
    "headers,blocked",
    [
        ({}, False),
        ({"Auto-Submitted": "no"}, False),
        ({"Auto-Submitted": "auto-replied"}, True),
        ({"Precedence": "bulk"}, True),
        ({"Precedence": "list"}, True),
        ({"precedence": "Junk"}, True),  # case-insensitive
    ],
)
def test_is_auto_submitted(headers, blocked):
    assert inbound.is_auto_submitted(headers) is blocked


def test_build_delivered_message_inlines_header_and_token():
    msg = inbound.build_delivered_message(
        from_addr="alice@example.com",
        subject="Need a hand",
        body_text="Can you draft a reply?",
        reply_token="TOKEN123",
    )
    assert "alice@example.com" in msg
    assert "Need a hand" in msg
    assert "TOKEN123" in msg
    assert "Can you draft a reply?" in msg
    # The body must come after the header block.
    assert msg.index("TOKEN123") < msg.index("Can you draft a reply?")


@pytest.mark.asyncio
async def test_fetch_received_email_ok(monkeypatch):
    def handler(request):
        assert request.url.path == "/emails/receiving/eml_1"
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"text": "hi", "message_id": "<m@x>", "headers": {}})

    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    got = await inbound.fetch_received_email("eml_1")
    assert got["text"] == "hi"


@pytest.mark.asyncio
async def test_fetch_received_email_non_200_returns_none(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")
    transport = httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "nope"}))
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.fetch_received_email("eml_missing") is None


@pytest.mark.asyncio
async def test_fetch_received_email_never_raises(monkeypatch):
    monkeypatch.setattr(inbound.settings, "RESEND_API_KEY", "re_test")

    def boom(r):
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(boom)
    monkeypatch.setattr(inbound, "_client_factory", lambda: httpx.AsyncClient(transport=transport))
    assert await inbound.fetch_received_email("eml_x") is None
```

- [ ] **Step 2: Run — expect FAIL.**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_inbound.py -v --no-cov -p no:cacheprovider`

- [ ] **Step 3: Implement `apps/backend/core/services/agent_mail_inbound.py`.** Provide a module-level `_client_factory` returning `httpx.AsyncClient(timeout=10)` so tests can inject a `MockTransport`. `parse_from_address` uses `email.utils.parseaddr`. `fetch_received_email` GETs the documented REST endpoint with `Authorization: Bearer {settings.RESEND_API_KEY}`, returns `resp.json()` on 200 else None, wrapped so it never raises. `build_delivered_message` produces, e.g.:

```
You received an email.

From: {from_addr}
Subject: {subject}
Reply-Token: {reply_token}

To reply, use the isol8-mail tool with the Reply-Token above.

---
{body_text}
```

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git add apps/backend/core/services/agent_mail_inbound.py apps/backend/tests/unit/services/test_agent_mail_inbound.py
git commit -m "feat(agent-email): inbound sender parsing, body fetch, message formatting"
```

---

### Task 4: deliver into the running container

**Files:**
- Create: `apps/backend/core/services/agent_mail_delivery.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_delivery.py`

**Interfaces:**
- Consumes: `get_ecs_manager().resolve_running_container(owner_id) -> (dict|None, str|None)` and `GATEWAY_PORT` (both in `core/containers/ecs_manager.py` — confirm import path; `hooks_proxy.py` is the reference caller); `httpx`.
- Produces:
  - `async deliver_to_agent(*, owner_id: str, agent_id: str, message: str, idempotency_key: str) -> str` — returns one of `"delivered"`, `"container_not_running"`, `"error"`. Never raises.

**Mirror `routers/hooks_proxy.py`:** resolve the running container, read `container["hook_token"]`, `httpx.post(f"http://{ip}:{GATEWAY_PORT}/hooks/agent", json={...}, headers={"Authorization": f"Bearer {hook_token}"})`. Body: `{"message": message, "agentId": agent_id, "name": "Email", "idempotencyKey": idempotency_key}`. No `sessionKey`. `ip is None` → return `"container_not_running"` (do NOT wake).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/services/test_agent_mail_delivery.py`:

```python
"""agent_mail_delivery: POST to the container /hooks/agent, or silent when down."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import agent_mail_delivery as delivery  # noqa: E402


def _fake_ecs(container, ip):
    m = AsyncMock()
    m.resolve_running_container = AsyncMock(return_value=(container, ip))
    return m


@pytest.mark.asyncio
async def test_delivers_to_running_container(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["json"] = request.read()
        return httpx.Response(200, json={"ok": True, "runId": "run_1"})

    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "tok_abc"}, "10.0.0.5"))
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=transport))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "delivered"
    assert "10.0.0.5" in captured["url"]
    assert captured["url"].endswith("/hooks/agent")
    assert captured["auth"] == "Bearer tok_abc"
    import json as _j
    body = _j.loads(captured["json"])
    assert body["message"] == "hi"
    assert body["agentId"] == "agent_friday"
    assert "sessionKey" not in body


@pytest.mark.asyncio
async def test_silent_when_container_not_running(monkeypatch):
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs(None, None))
    posted = {"called": False}

    def handler(request):
        posted["called"] = True
        return httpx.Response(200)

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "container_not_running"
    assert posted["called"] is False  # never POSTed, never woke


@pytest.mark.asyncio
async def test_error_on_post_failure_never_raises(monkeypatch):
    monkeypatch.setattr(delivery, "get_ecs_manager", lambda: _fake_ecs({"hook_token": "t"}, "10.0.0.5"))

    def boom(r):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(delivery, "_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    result = await delivery.deliver_to_agent(
        owner_id="org_1", agent_id="agent_friday", message="hi", idempotency_key="eml_1"
    )
    assert result == "error"
```

- [ ] **Step 2: Run — expect FAIL.**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_delivery.py -v --no-cov -p no:cacheprovider`

- [ ] **Step 3: Implement.** Module-level `_client_factory` = `lambda: httpx.AsyncClient(timeout=10)`. Import `get_ecs_manager` from `core.containers` and `GATEWAY_PORT` from `core.containers.ecs_manager` (confirm both by grep). Wrap the whole thing so it never raises (return `"error"`).

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git add apps/backend/core/services/agent_mail_delivery.py apps/backend/tests/unit/services/test_agent_mail_delivery.py
git commit -m "feat(agent-email): deliver inbound message into container via /hooks/agent"
```

---

### Task 5: the inbound webhook router

**Files:**
- Create: `apps/backend/routers/agent_mail.py`
- Modify: `apps/backend/main.py` (import + mount beside the other webhook routers)
- Test: `apps/backend/tests/unit/routers/test_agent_mail.py`

**Interfaces:**
- Consumes: everything above, plus `agent_email_repo.get_by_address` / `normalize`, `trial_cap.trial_block_reason`, `webhook_dedup.record_event_or_skip`, `core.crypto.svix.verify_svix_signature`.
- Produces: `POST /api/v1/webhooks/resend-inbound`, returning fast 200 with a `{"status": …}` body for observability (`disabled` / `duplicate` / `bad_signature`→401 / `dropped_*` / `delivered` / `container_not_running`).

**Orchestration (all inside try/except; never 5xx):**
1. If `RESEND_WEBHOOK_SECRET` not `whsec_…` OR `AGENT_EMAIL_DOMAIN` empty → `200 {"status":"disabled"}`.
2. `body = await request.body()`; `verify_svix_signature(...)` → False ⇒ `401`.
3. `record_event_or_skip(svix-id, source="resend_inbound")` → duplicate ⇒ `200 {"status":"duplicate"}`.
4. Parse JSON; require `type == "email.received"`; pull `data.email_id`, `data.to` (list), `data.from`, `data.subject`, `data.message_id`.
5. Resolve recipient: for each addr in `data.to`, `get_by_address(normalize(addr))`; first hit → `(row)`. None → `200 {"status":"dropped_unknown_recipient"}`.
6. Sender: `parse_from_address(data.from)`; if not `row["public"]` and sender ∉ `row["verified_senders"]` → `200 {"status":"dropped_unverified_sender"}`.
7. Cap: `await trial_block_reason(row["owner_id"])` not None → `200 {"status":"dropped_over_cap"}`.
8. `fetch_received_email(email_id)`; None → `200 {"status":"dropped_fetch_failed"}`. Loop guard: `is_auto_submitted(fetched["headers"] or {})` → `200 {"status":"dropped_auto_submitted"}`.
9. Build reply token from `{owner_id, agent_id, reply_to: sender, in_reply_to: data.message_id, subject}`; `build_delivered_message(...)` with `fetched["text"]` (fallback to a stripped `html` or empty).
10. `deliver_to_agent(owner_id, agent_id, message, idempotency_key=email_id)` → return its status.

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/routers/test_agent_mail.py`. Use `TestClient`, monkeypatch `verify_svix_signature`→True (signature has its own unit test), and monkeypatch the repo/gate/fetch/deliver seams. Cover: disabled (secret unset), bad signature → 401, duplicate, unknown recipient, unverified sender, over cap, auto-submitted drop, and the happy path asserting `deliver_to_agent` was called with a message containing the sender + a non-empty reply token. (Write one test per branch; assert the JSON `status` and that `deliver_to_agent` is called only on the happy path.) Follow `tests/unit/routers/teams/test_settings.py` for the `TestClient` + dependency-override style and `tests/unit/routers/test_metronome_webhook.py` for webhook-body posting.

- [ ] **Step 2: Run — expect FAIL.**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_agent_mail.py -v --no-cov -p no:cacheprovider`

- [ ] **Step 3: Implement `routers/agent_mail.py`** per the orchestration above, mirroring `routers/webhooks.py` structure (public router, raw body, fast 200, best-effort try/except so a bug returns 200 not 500). Mount in `main.py` beside the Clerk webhook: `app.include_router(agent_mail.router, prefix="/api/v1/webhooks", tags=["webhooks"])`.

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git add apps/backend/routers/agent_mail.py apps/backend/main.py apps/backend/tests/unit/routers/test_agent_mail.py
git commit -m "feat(agent-email): inbound Resend webhook router"
```

---

### Task 6: full-suite verification

- [ ] **Step 1:** `cd apps/backend && uv run pytest tests/ -q --no-cov -p no:cacheprovider` — expect only the one known pre-existing failure. (Full scope, NOT `tests/unit`. It exceeds 10 min; let it background and read the summary line — do not trust a piped exit code.)
- [ ] **Step 2:** Confirm still inert by default: `grep -rn "RESEND_WEBHOOK_SECRET\|AGENT_EMAIL_DOMAIN" apps/infra/lib || echo "unset — inert"`.
- [ ] **Step 3:** `git add -A && git commit -m "chore(agent-email): inbound lint fixups" || echo "nothing to commit"`.

---

## Done — What Ships

The inbound half, still inert (no `RESEND_WEBHOOK_SECRET` / `AGENT_EMAIL_DOMAIN` in any env). When a later runbook verifies the Resend receiving domain and sets both secrets, an owner-verified sender's email reaches a *running* container's agent as a conversation turn carrying an opaque reply token. Asleep containers stay silent.

**Next (Plan 3, Outbound):** the relay endpoint that decodes the reply token and sends the agent's reply via Resend, plus the baked `isol8-mail` tool in the OpenClaw image. `agent_mail_token.decode_reply_token` is its entry point.
