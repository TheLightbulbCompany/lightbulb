# Agent Mail Inbox / Allowlisted Wake / Free Send — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any agent can use its `{agent}@{slug}.isol8.co` address as a real identity — read its own inbox (OTPs), be woken only by owner-allowlisted senders, and email anyone the owner permits — on a per-owner SES tenant with a complaint feedback loop.

**Architecture:** Inbound mail that passes the existing SES gates is STORED in a new `agent-mail-inbox` DynamoDB table and the agent is WOKEN only for allowlisted senders. ONE outbound door (`POST /agent-mail/u/{owner_id}/send`) with a server-side policy (owner / reply / free) replaces the reply-token relay and the owner-send route; one CLI (`isol8-mail.sh address|inbox|send`) replaces two scripts. SES: per-env configuration set + EventBridge complaint → API destination → backend; per-owner tenants created lazily by the backend.

**Tech Stack:** FastAPI + boto3 (sesv2 1.42.68, DynamoDB), pytest; AWS CDK 2.190 (`aws-ses` L1s, `aws-events` Connection/ApiDestination); bash skill script; Next.js/SWR component.

**Spec:** `docs/superpowers/specs/2026-08-21-agent-mail-inbox-design.md` (same worktree). The spec's section 1/2/4 said "S3 prefix on the attachments bucket" — this plan uses a **DynamoDB table** instead: the attachments bucket is prod-only (`isol8-stage.ts` only instantiates `SesInboundStack` for prod, so dev has no bucket env) and the backend has no IAM on it, while a per-env table needs one `createTable` + one grant. Update the spec's wording in the shipping PR (or delete it — it is disposable).

## Global Constraints

- Worktree: `/Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.worktrees/agent-mail-inbox` (branch `feat/agent-mail-inbox`). Use this absolute path in every command. Never `git stash`.
- Backend tests: `cd apps/backend && uv run pytest <path> -v` (hermetic; fake AWS env is set by conftest). Full suite at the end: `uv run pytest tests/`.
- Infra: `cd apps/infra && pnpm exec tsc --noEmit` after every CDK edit; `pnpm exec jest` if `apps/infra/test` exists (check with `ls`).
- Frontend: `cd apps/frontend && pnpm exec tsc --noEmit` + `pnpm exec eslint <file>`.
- No backwards-compat shims: delete replaced routes/scripts/modules outright.
- `DAILY_SEND_CAP` becomes 50 (was 10). Chaos `NEGATIVE_PASS_EXCLUSIONS` must list every new `/u/{owner_id}` route.
- Commit after every task: `git add <explicit paths>` (never `-A`), message prefix `feat(agent-mail): …`, trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Every new settings field lives in `apps/backend/core/config.py` `Settings`.

---

## File structure

| Path | Responsibility |
|---|---|
| `apps/backend/core/repositories/agent_mail_inbox_repo.py` (new) | stored inbound messages: `put_message`, `list_messages`, `get_message`, `parse_reply_key`, `reply_key` |
| `apps/backend/core/repositories/agent_email_repo.py` | address rows: add `update_settings`; drop `public` from `put` |
| `apps/backend/core/services/agent_email.py` | mint + `sender_allowed` + `ensure_agent_address` (any agent, live names) + `resolve_company_name` (moved from owner_send) |
| `apps/backend/core/services/agent_mail_tenant.py` (new) | `tenant_name_for`, `ensure_tenant` (SES per-owner tenant) |
| `apps/backend/core/services/agent_mail_send.py` (new) | ONE send door: policy + dispatch (owner / reply / free) |
| `apps/backend/core/services/agent_mail_owner_send.py` | owner path; gains `agent_id`/`tenant_name` kwargs; `DAILY_SEND_CAP=50`; mint via `ensure_agent_address` |
| `apps/backend/core/services/agent_mail_reply.py` | unchanged helpers; `send_reply` gains `tenant_name` kwarg |
| `apps/backend/core/services/agent_mail_inbound.py` | `build_delivered_message(reply_key=…)` replaces `reply_token` |
| `apps/backend/core/services/ses_email.py` | `tenant_name` kwarg + `ConfigurationSetName` from settings |
| `apps/backend/routers/agent_mail.py` (new) | `/u/{owner_id}/send`, `/u/{owner_id}/address`, `/u/{owner_id}/inbox` (hook-token); `/addresses` GET, `/addresses/{agent_id}` PATCH (Clerk) |
| `apps/backend/routers/ses_inbound.py` | store-then-wake; `POST /ses-events` complaint handler |
| DELETE | `routers/agent_mail_relay.py`, `routers/agent_mail_send.py`, `core/services/agent_mail_token.py`, their tests, `skills/isol8/scripts/isol8-owner-email.sh` |
| `apps/infra/lib/stacks/database-stack.ts` | `agentMailInboxTable` |
| `apps/infra/lib/stacks/agent-mail-outbound-stack.ts` (new) | config set, complaint event destination, rule, connection, API destination |
| `apps/infra/lib/stacks/service-stack.ts` | inbox grant, `AGENT_MAIL_CONFIGURATION_SET` env, SES tenant IAM |
| `apps/infra/lib/isol8-stage.ts` | wire the two above |
| `apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh` | ONE CLI: `address | inbox | send` |
| `apps/infra/openclaw/skills/isol8/SKILL.md` | email sections rewritten |
| `apps/frontend/src/components/control/panels/AgentEmailSection.tsx` (new) + `AgentsPanel.tsx` | Email tab |

---

### Task 1: Inbox table + repo

**Files:**
- Modify: `apps/infra/lib/stacks/database-stack.ts` (after the `agentEmailAddressesTable` block ~L170-180), `apps/infra/lib/stacks/service-stack.ts` (database iface ~L112-130; grant after L784), `apps/infra/lib/isol8-stage.ts` (database props ~L125-143)
- Create: `apps/backend/core/repositories/agent_mail_inbox_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_agent_mail_inbox_repo.py`

**Interfaces:**
- Produces: table `isol8-{env}-agent-mail-inbox` (pk `address` S, sk `received_key` S, TTL `ttl`).
- Produces (Python):
  - `reply_key(address: str, received_key: str) -> str` → `"{address}/{received_key}"`
  - `parse_reply_key(key: str) -> tuple[str, str] | None`
  - `async put_message(*, address, owner_id, agent_id, message_id, from_addr, subject, text, headers: dict, attachments: list, received_at: str | None = None) -> dict` (item; `text` truncated to 64_000 chars; `ttl` = now + 30d epoch)
  - `async list_messages(address, *, since: str | None = None, limit: int = 20) -> list[dict]` newest-first
  - `async get_message(address, received_key) -> dict | None`

- [ ] **Step 1: CDK table.** In `database-stack.ts` add a public field `agentMailInboxTable: dynamodb.Table;` next to `agentEmailAddressesTable` and, right after the `agentEmailAddressesTable` GSI block:

```ts
    // Stored inbound agent mail (inbox the agent reads on demand — OTPs etc.).
    // PK = the agent's address, SK = "{received_at}#{message_id}" so a query
    // is newest-first by ISO timestamp. 30-day TTL: the inbox is a working
    // buffer, not an archive.
    this.agentMailInboxTable = createTable("AgentMailInboxTable", "agent-mail-inbox", {
      partitionKey: { name: "address", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "received_key", type: dynamodb.AttributeType.STRING },
      timeToLiveAttribute: "ttl",
      ephemeral: true,
    });
```

In `service-stack.ts` add `agentMailInboxTable: dynamodb.Table;` to the `database` prop interface and, directly after `props.database.agentEmailAddressesTable.grantReadWriteData(this.taskRole);`:

```ts
    // Stored inbound agent mail — read by the inbox route, written by ses_inbound.
    props.database.agentMailInboxTable.grantReadWriteData(this.taskRole);
```

In `isol8-stage.ts` add `agentMailInboxTable: database.agentMailInboxTable,` after `agentEmailAddressesTable: database.agentEmailAddressesTable,`.

- [ ] **Step 2: typecheck infra.** Run: `cd apps/infra && pnpm exec tsc --noEmit`. Expected: clean.

- [ ] **Step 3: failing repo test.** Create `apps/backend/tests/unit/repositories/test_agent_mail_inbox_repo.py`:

```python
"""agent_mail_inbox_repo: key shape, truncation, newest-first listing."""

from unittest.mock import MagicMock

import pytest

from core.repositories import agent_mail_inbox_repo as repo


def test_reply_key_roundtrip():
    key = repo.reply_key("friday@acme.isol8.co", "2026-08-21T10:00:00+00:00#m1")
    assert key == "friday@acme.isol8.co/2026-08-21T10:00:00+00:00#m1"
    assert repo.parse_reply_key(key) == ("friday@acme.isol8.co", "2026-08-21T10:00:00+00:00#m1")


def test_parse_reply_key_rejects_garbage():
    assert repo.parse_reply_key("") is None
    assert repo.parse_reply_key("no-slash") is None
    assert repo.parse_reply_key("/leading") is None


@pytest.mark.asyncio
async def test_put_message_truncates_text_and_sets_ttl(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.put_message(
        address="Friday@Acme.isol8.co",
        owner_id="user_1",
        agent_id="agent_1",
        message_id="m1",
        from_addr="noreply@github.com",
        subject="Verify",
        text="x" * 70_000,
        headers={"Message-ID": "<a@b>"},
        attachments=[],
        received_at="2026-08-21T10:00:00+00:00",
    )
    assert item["address"] == "friday@acme.isol8.co"
    assert item["received_key"] == "2026-08-21T10:00:00+00:00#m1"
    assert len(item["text"]) == 64_000
    assert item["ttl"] > 0
    table.put_item.assert_called_once()


@pytest.mark.asyncio
async def test_list_messages_queries_newest_first(monkeypatch):
    table = MagicMock()
    table.query.return_value = {"Items": [{"received_key": "b"}, {"received_key": "a"}]}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    rows = await repo.list_messages("friday@acme.isol8.co", since="2026-08-20T00:00:00+00:00", limit=5)
    assert [r["received_key"] for r in rows] == ["b", "a"]
    kwargs = table.query.call_args.kwargs
    assert kwargs["ScanIndexForward"] is False
    assert kwargs["Limit"] == 5
```

- [ ] **Step 4: run, verify fail.** `cd apps/backend && uv run pytest tests/unit/repositories/test_agent_mail_inbox_repo.py -v` → ImportError / AttributeError.

- [ ] **Step 5: implement** `apps/backend/core/repositories/agent_mail_inbox_repo.py`:

```python
"""Stored inbound agent mail — the inbox an agent reads on demand.

One row per delivered-or-stored inbound message. PK is the agent's address
(mail is routed by recipient), SK is ``{received_at}#{message_id}`` so the
base-table query is newest-first by ISO timestamp. 30-day TTL.

The ``reply_key`` (``"{address}/{received_key}"``) is what the delivered chat
message carries and what ``isol8-mail.sh send --reply`` hands back — it is
NOT a credential: the send door checks the row's ``owner_id`` against the
hook-token-authenticated owner before it is honoured.
"""

import time

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_MAX_TEXT_CHARS = 64_000
_TTL_SECONDS = 30 * 24 * 3600


def _get_table():
    return get_table("agent-mail-inbox")


def _normalize(value: str) -> str:
    return value.strip().lower()


def reply_key(address: str, received_key: str) -> str:
    return f"{_normalize(address)}/{received_key}"


def parse_reply_key(key: str) -> tuple[str, str] | None:
    if not key or "/" not in key:
        return None
    address, _, received_key = key.partition("/")
    if not address or not received_key:
        return None
    return _normalize(address), received_key


async def put_message(
    *,
    address: str,
    owner_id: str,
    agent_id: str,
    message_id: str,
    from_addr: str,
    subject: str,
    text: str,
    headers: dict,
    attachments: list,
    received_at: str | None = None,
) -> dict:
    received_at = received_at or utc_now_iso()
    item = {
        "address": _normalize(address),
        "received_key": f"{received_at}#{message_id or 'no-id'}",
        "owner_id": owner_id,
        "agent_id": agent_id,
        "message_id": message_id,
        "from_addr": from_addr,
        "subject": subject,
        "text": (text or "")[:_MAX_TEXT_CHARS],
        # Only the threading headers — the full header dict is sender-controlled bulk.
        "headers": {
            k: v
            for k, v in (headers or {}).items()
            if k.lower() in ("message-id", "references", "in-reply-to")
        },
        "attachments": attachments or [],
        "received_at": received_at,
        "ttl": int(time.time()) + _TTL_SECONDS,
    }
    await run_in_thread(_get_table().put_item, Item=item)
    return item


async def list_messages(address: str, *, since: str | None = None, limit: int = 20) -> list[dict]:
    cond = Key("address").eq(_normalize(address))
    if since:
        cond = cond & Key("received_key").gt(since)
    resp = await run_in_thread(
        _get_table().query,
        KeyConditionExpression=cond,
        ScanIndexForward=False,
        Limit=max(1, min(int(limit), 50)),
    )
    return resp.get("Items", [])


async def get_message(address: str, received_key: str) -> dict | None:
    resp = await run_in_thread(
        _get_table().get_item, Key={"address": _normalize(address), "received_key": received_key}
    )
    return resp.get("Item")
```

- [ ] **Step 6: run, verify pass.** Same command → 4 passed.

- [ ] **Step 7: commit.**
```bash
git add apps/infra/lib/stacks/database-stack.ts apps/infra/lib/stacks/service-stack.ts apps/infra/lib/isol8-stage.ts apps/backend/core/repositories/agent_mail_inbox_repo.py apps/backend/tests/unit/repositories/test_agent_mail_inbox_repo.py
git commit -m "feat(agent-mail): inbox table + repo"
```

---

### Task 2: Address-row settings + `@domain` sender matching

**Files:**
- Modify: `apps/backend/core/repositories/agent_email_repo.py` (`put`: drop `public`; add `update_settings`), `apps/backend/core/services/agent_email.py` (add `sender_allowed`)
- Test: `apps/backend/tests/unit/services/test_agent_email.py` (append), `apps/backend/tests/unit/repositories/test_agent_email_repo.py` (create if absent; `ls tests/unit/repositories/` first)

**Interfaces:**
- Produces: `agent_email.sender_allowed(row: dict, sender: str) -> bool` — True if `sender` equals an entry in `row["verified_senders"]` or the entry is `@domain` and `sender` ends with `@domain` (case-insensitive).
- Produces: `agent_email_repo.update_settings(address, *, verified_senders: list[str] | None = None, send_to_anyone: bool | None = None, outbound_disabled: str | None | object = UNSET) -> dict | None` — UpdateItem SET for the supplied fields only; `outbound_disabled=None` REMOVEs the attribute; returns the updated item (`ReturnValues="ALL_NEW"`), or None when the row does not exist (`attribute_exists(address)` condition).
- `put` signature becomes `put(*, address, owner_id, agent_id, verified_senders)` — no `public`, and the item no longer carries `public`.

- [ ] **Step 1: failing tests.** Append to `tests/unit/services/test_agent_email.py`:

```python
from core.services.agent_email import sender_allowed


def test_sender_allowed_exact_and_domain():
    row = {"verified_senders": ["owner@example.com", "@clientco.com"]}
    assert sender_allowed(row, "owner@example.com")
    assert sender_allowed(row, "Owner@Example.com")
    assert sender_allowed(row, "anyone@clientco.com")
    assert not sender_allowed(row, "anyone@notclientco.com")
    assert not sender_allowed(row, "anyone@sub.clientco.com")
    assert not sender_allowed({}, "owner@example.com")
```

Create `tests/unit/repositories/test_agent_email_repo.py`:

```python
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.repositories import agent_email_repo as repo


@pytest.mark.asyncio
async def test_update_settings_sets_only_supplied_fields(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c", "send_to_anyone": True}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    out = await repo.update_settings("A@b.c", send_to_anyone=True)
    assert out["send_to_anyone"] is True
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"address": "a@b.c"}
    assert "send_to_anyone" in kwargs["UpdateExpression"]
    assert "verified_senders" not in kwargs["UpdateExpression"]
    assert kwargs["ConditionExpression"] == "attribute_exists(address)"


@pytest.mark.asyncio
async def test_update_settings_none_removes_outbound_disabled(monkeypatch):
    table = MagicMock()
    table.update_item.return_value = {"Attributes": {"address": "a@b.c"}}
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    await repo.update_settings("a@b.c", outbound_disabled=None)
    assert "REMOVE outbound_disabled" in table.update_item.call_args.kwargs["UpdateExpression"]


@pytest.mark.asyncio
async def test_update_settings_missing_row_returns_none(monkeypatch):
    table = MagicMock()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    assert await repo.update_settings("a@b.c", send_to_anyone=False) is None


@pytest.mark.asyncio
async def test_put_has_no_public_field(monkeypatch):
    table = MagicMock()
    monkeypatch.setattr(repo, "_get_table", lambda: table)
    item = await repo.put(address="a@b.c", owner_id="o", agent_id="ag", verified_senders=["O@x.y"])
    assert "public" not in item
    assert item["verified_senders"] == ["o@x.y"]
```

- [ ] **Step 2: run, verify fail.** `uv run pytest tests/unit/services/test_agent_email.py tests/unit/repositories/test_agent_email_repo.py -v`.

- [ ] **Step 3: implement.** In `agent_email_repo.py`: remove the `public` parameter and the `"public": public,` line from `put`; add:

```python
_UNSET = object()


async def update_settings(
    address: str,
    *,
    verified_senders: list[str] | None = None,
    send_to_anyone: bool | None = None,
    outbound_disabled: str | None | object = _UNSET,
) -> dict | None:
    """SET the supplied owner-editable fields on an existing row; None for
    ``outbound_disabled`` REMOVEs it. Returns the updated item, or None when
    the row does not exist (conditional on ``attribute_exists(address)``)."""
    sets: list[str] = []
    removes: list[str] = []
    values: dict = {}
    if verified_senders is not None:
        sets.append("verified_senders = :vs")
        values[":vs"] = [normalize(s) for s in verified_senders]
    if send_to_anyone is not None:
        sets.append("send_to_anyone = :sta")
        values[":sta"] = bool(send_to_anyone)
    if outbound_disabled is not _UNSET:
        if outbound_disabled is None:
            removes.append("outbound_disabled")
        else:
            sets.append("outbound_disabled = :od")
            values[":od"] = str(outbound_disabled)
    if not sets and not removes:
        return await get_by_address(address)
    expr = ""
    if sets:
        expr += "SET " + ", ".join(sets)
    if removes:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(removes)
    kwargs = {
        "Key": {"address": normalize(address)},
        "UpdateExpression": expr,
        "ConditionExpression": "attribute_exists(address)",
        "ReturnValues": "ALL_NEW",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        resp = await run_in_thread(_get_table().update_item, **kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")
```

In `agent_email.py` add:

```python
def sender_allowed(row: dict, sender: str) -> bool:
    """True when ``sender`` may WAKE this address's agent: an exact entry in
    ``verified_senders`` or an ``@domain`` entry matching the sender's domain
    exactly (no subdomain matching — ``@clientco.com`` does not admit
    ``x@sub.clientco.com``)."""
    sender = (sender or "").strip().lower()
    if not sender or "@" not in sender:
        return False
    domain = "@" + sender.rsplit("@", 1)[1]
    for entry in row.get("verified_senders") or []:
        entry = (entry or "").strip().lower()
        if not entry:
            continue
        if entry.startswith("@"):
            if entry == domain:
                return True
        elif entry == sender:
            return True
    return False
```

- [ ] **Step 4: run, verify pass.** Then run `uv run pytest tests/unit/services/test_agent_email.py tests/unit/routers/test_ses_inbound.py -q` — `test_ses_inbound.py` still passes (it builds rows with `public=` in `_row` but that is test-local; the router reads `row.get("public")` until Task 3).

- [ ] **Step 5: commit.**
```bash
git add apps/backend/core/repositories/agent_email_repo.py apps/backend/core/services/agent_email.py apps/backend/tests/unit/services/test_agent_email.py apps/backend/tests/unit/repositories/test_agent_email_repo.py
git commit -m "feat(agent-mail): address settings update + @domain sender matching"
```

---

### Task 3: Inbound — store everything, wake only allowlisted; Reply-Key replaces the token

**Files:**
- Modify: `apps/backend/routers/ses_inbound.py`, `apps/backend/core/services/agent_mail_inbound.py`
- Delete: `apps/backend/core/services/agent_mail_token.py`, `apps/backend/tests/unit/services/test_agent_mail_token.py`
- Test: `apps/backend/tests/unit/routers/test_ses_inbound.py`, `apps/backend/tests/unit/services/test_agent_mail_inbound.py`

**Interfaces:**
- `agent_mail_inbound.build_delivered_message(*, from_addr, subject, body_text, reply_key: str, attachments_manifest="")` — header line becomes `Reply-Key: {reply_key}` and the hint line `To reply, run: /app/skills/isol8/scripts/isol8-mail.sh send --reply <the Reply-Key> (body on stdin).`
- `ses_inbound` outcomes: new terminal status `stored_not_woken`; `dropped_unverified_sender` is gone.

- [ ] **Step 1: update the inbound-helper test.** In `tests/unit/services/test_agent_mail_inbound.py` replace every `reply_token=` kwarg with `reply_key=` and every `Reply-Token:` assertion with `Reply-Key:`; add:

```python
def test_delivered_message_names_the_reply_command():
    msg = build_delivered_message(from_addr="a@b.c", subject="S", body_text="B", reply_key="a@b.c/2026#m1")
    assert "Reply-Key: a@b.c/2026#m1" in msg
    assert "isol8-mail.sh send --reply" in msg
```

- [ ] **Step 2: update the router tests.** In `tests/unit/routers/test_ses_inbound.py`:
  - `_row`: drop the `public` parameter/field.
  - Add a fixture that stubs the store: `monkeypatch.setattr(ses_inbound, "put_message", AsyncMock(return_value={"address": RECIPIENT, "received_key": "2026#m"}))` inside `_dedup_recorded` (every test that reaches the recipient gate needs it — simplest: put it in `_dedup_recorded`).
  - Replace the unverified-sender test(s) (the ones asserting `dropped_unverified_sender` and the `public` bypass) with:

```python
def test_unlisted_sender_is_stored_not_woken(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row(verified_senders=["owner@x.y"])))
    deliver = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver)
    r = _post(client, _body())  # SENDER is not owner@x.y
    assert r.status_code == 200 and r.json()["status"] == "stored_not_woken"
    ses_inbound.put_message.assert_awaited_once()
    deliver.assert_not_awaited()


def test_domain_allowlist_entry_wakes(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row(verified_senders=["@example.com"])))
    monkeypatch.setattr(ses_inbound, "trial_block_reason", AsyncMock(return_value=None))
    deliver = AsyncMock(return_value="delivered")
    monkeypatch.setattr(ses_inbound, "deliver_to_agent", deliver)
    r = _post(client, _body())
    assert r.json()["status"] == "delivered"
    assert "Reply-Key: " in deliver.call_args.kwargs["message"]


def test_spam_is_dropped_before_store(client, monkeypatch):
    _enable(monkeypatch)
    _dedup_recorded(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    r = _post(client, _body(verdicts={"spam": "FAIL"}))
    assert r.json()["status"] == "dropped_spam_or_virus"
    ses_inbound.put_message.assert_not_awaited()
```

  - Any existing test asserting `Reply-Token` in the delivered message → `Reply-Key`. Any test importing `encode_reply_token` → remove.

- [ ] **Step 3: run, verify fail.** `uv run pytest tests/unit/routers/test_ses_inbound.py tests/unit/services/test_agent_mail_inbound.py -q`.

- [ ] **Step 4: implement.** `agent_mail_inbound.build_delivered_message`: rename param to `reply_key`, header block:

```python
    message = (
        "You received an email.\n"
        "\n"
        f"From: {from_addr}\n"
        f"Subject: {subject}\n"
        f"Reply-Key: {reply_key}\n"
        "\n"
        "To reply, run: /app/skills/isol8/scripts/isol8-mail.sh send --reply <the Reply-Key> "
        "(reply body on stdin).\n"
        "\n"
        "---\n"
        f"{body_text}"
    )
```

`routers/ses_inbound.py`:
  - imports: drop `encode_reply_token`; add `from core.repositories.agent_mail_inbox_repo import put_message, reply_key` and `from core.services.agent_email import sender_allowed`.
  - Delete the "Sender allowlist" block (the `if not row.get("public") and sender not in ...` return).
  - Keep spam/virus, DMARC, auto-submitted gates as they are (they now run for every sender — that is the point).
  - Replace the cap block's `if not row.get("public"):` with `if sender_allowed(row, sender):` — the autoreply stays allowlist-only (same reflector argument as before).
  - After the cap block, replace the `token = encode_reply_token(...)` block with:

```python
        stored = await put_message(
            address=row["address"],
            owner_id=row["owner_id"],
            agent_id=row["agent_id"],
            message_id=body.message_id,
            from_addr=sender,
            subject=body.subject or "",
            text=body.text or "",
            headers=body.headers or {},
            attachments=body.attachments or [],
        )
        if not sender_allowed(row, sender):
            return _final("stored_not_woken")
        key = reply_key(row["address"], stored["received_key"])
```

  - `build_delivered_message(..., reply_key=key, ...)`.
  - Add `"stored_not_woken"` to `_METRIC_RESULTS`.
  - Delete `core/services/agent_mail_token.py` and `tests/unit/services/test_agent_mail_token.py` (`git rm`). `grep -rn agent_mail_token apps/backend` must return only `routers/agent_mail_relay.py` (deleted in Task 8).

- [ ] **Step 5: run, verify pass.** Same command + `uv run pytest tests/unit/services/test_agent_mail_inbound.py -q`.

- [ ] **Step 6: commit.**
```bash
git add -u apps/backend/routers/ses_inbound.py apps/backend/core/services/agent_mail_inbound.py apps/backend/core/services/agent_mail_token.py apps/backend/tests/unit/services/test_agent_mail_token.py apps/backend/tests/unit/routers/test_ses_inbound.py apps/backend/tests/unit/services/test_agent_mail_inbound.py
git commit -m "feat(agent-mail): store all inbound, wake only allowlisted senders, Reply-Key"
```

---

### Task 4: SES send stamps configuration set + tenant

**Files:**
- Modify: `apps/backend/core/config.py` (next to `AGENT_EMAIL_DOMAIN`), `apps/backend/core/services/ses_email.py`
- Test: `apps/backend/tests/unit/services/test_ses_email.py` (append)

**Interfaces:**
- `Settings.AGENT_MAIL_CONFIGURATION_SET: str = ""` — SES configuration set name (`isol8-{env}-agent-mail`); empty ⇒ omitted from sends.
- `send_ses_email(..., tenant_name: str | None = None)` and `send_ses_reply(..., tenant_name: str | None = None)` — when set, `send_email(..., TenantName=tenant_name)`; when `AGENT_MAIL_CONFIGURATION_SET` set, `ConfigurationSetName=...`.

- [ ] **Step 1: failing test.** Append to `tests/unit/services/test_ses_email.py` (look at its existing fixture for how `_client` is monkeypatched — reuse it):

```python
@pytest.mark.asyncio
async def test_send_stamps_configuration_set_and_tenant(monkeypatch):
    from core.services import ses_email
    monkeypatch.setattr(ses_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(ses_email.settings, "AGENT_MAIL_CONFIGURATION_SET", "isol8-dev-agent-mail")
    fake = MagicMock()
    fake.send_email.return_value = {"MessageId": "m"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake)
    await ses_email.send_ses_reply(from_addr="f@a.isol8.co", to="t@x.y", subject="s", body_text="b", tenant_name="owner-user_1")
    kwargs = fake.send_email.call_args.kwargs
    assert kwargs["ConfigurationSetName"] == "isol8-dev-agent-mail"
    assert kwargs["TenantName"] == "owner-user_1"


@pytest.mark.asyncio
async def test_send_omits_stamps_when_unset(monkeypatch):
    from core.services import ses_email
    monkeypatch.setattr(ses_email.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(ses_email.settings, "AGENT_MAIL_CONFIGURATION_SET", "")
    fake = MagicMock()
    fake.send_email.return_value = {"MessageId": "m"}
    monkeypatch.setattr(ses_email, "_client", lambda: fake)
    await ses_email.send_ses_reply(from_addr="f@a.isol8.co", to="t@x.y", subject="s", body_text="b")
    kwargs = fake.send_email.call_args.kwargs
    assert "ConfigurationSetName" not in kwargs and "TenantName" not in kwargs
```

- [ ] **Step 2: run, verify fail.**

- [ ] **Step 3: implement.** `config.py`:

```python
    # SES configuration set every agent-mail send is stamped with (the
    # complaint event destination hangs off it). Empty ⇒ sends carry no
    # configuration set — fine for envs without the outbound stack.
    AGENT_MAIL_CONFIGURATION_SET: str = ""
```

`ses_email.send_ses_email`: add `tenant_name: str | None = None` kwarg; build the call:

```python
        send_kwargs: dict = {"Content": {"Raw": {"Data": raw}}}
        if settings.AGENT_MAIL_CONFIGURATION_SET.strip():
            send_kwargs["ConfigurationSetName"] = settings.AGENT_MAIL_CONFIGURATION_SET.strip()
        if tenant_name:
            send_kwargs["TenantName"] = tenant_name
        ...
                lambda: _client().send_email(**send_kwargs),
```

`send_ses_reply`: add `tenant_name: str | None = None`, pass through.

- [ ] **Step 4: run, verify pass.** `uv run pytest tests/unit/services/test_ses_email.py -q`.

- [ ] **Step 5: commit.** `git add apps/backend/core/config.py apps/backend/core/services/ses_email.py apps/backend/tests/unit/services/test_ses_email.py && git commit -m "feat(agent-mail): stamp SES configuration set + tenant on sends"`

---

### Task 5: Per-owner SES tenant

**Files:**
- Create: `apps/backend/core/services/agent_mail_tenant.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_tenant.py`

**Interfaces:**
- `tenant_name_for(owner_id: str) -> str` → `"owner-" + re.sub(r"[^A-Za-z0-9_-]", "-", owner_id)[:58]`
- `async ensure_tenant(owner_id: str) -> str | None` — returns the tenant name; None when `AGENT_MAIL_CONFIGURATION_SET` is empty (feature off) or on any AWS error (never raises). Idempotent: `create_tenant` (ignore `AlreadyExistsException`), `create_tenant_resource_association` for the `isol8.co` identity ARN and the configuration-set ARN (ignore `AlreadyExistsException`), `update_reputation_entity_policy(ReputationEntityType="RESOURCE", ReputationEntityReference=<tenant ARN>, ReputationEntityPolicy=f"arn:aws:ses:{region}:aws:reputation-policy/standard")`. In-process `_ensured: set[str]` memo (`# ponytail: per-process memo; re-runs the idempotent calls after a restart`).
- Identity = `settings.AGENT_EMAIL_DOMAIN` (e.g. `isol8.co`, `dev.isol8.co` — NOTE dev's identity is still `isol8.co`; see `_identity_domain()` below).

- [ ] **Step 1: failing test.**

```python
"""agent_mail_tenant: deterministic names, idempotent creation, fail-soft."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import agent_mail_tenant as mod


def test_tenant_name_sanitizes():
    assert mod.tenant_name_for("user_2abc") == "owner-user_2abc"
    assert mod.tenant_name_for("org_2abc") == "owner-org_2abc"
    assert mod.tenant_name_for("weird id!") == "owner-weird-id-"


@pytest.fixture
def ses(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(mod, "_client", lambda: fake)
    monkeypatch.setattr(mod, "_account_id", lambda: "123456789012")  # sync in tests; see impl
    monkeypatch.setattr(mod.settings, "AGENT_MAIL_CONFIGURATION_SET", "isol8-dev-agent-mail")
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "dev.isol8.co")
    monkeypatch.setattr(mod.settings, "AWS_REGION", "us-east-1")
    mod._ensured.clear()
    return fake


@pytest.mark.asyncio
async def test_ensure_tenant_creates_associates_and_sets_policy(ses):
    name = await mod.ensure_tenant("user_1")
    assert name == "owner-user_1"
    ses.create_tenant.assert_called_once_with(TenantName="owner-user_1")
    arns = {c.kwargs["ResourceArn"] for c in ses.create_tenant_resource_association.call_args_list}
    assert arns == {
        "arn:aws:ses:us-east-1:123456789012:identity/isol8.co",
        "arn:aws:ses:us-east-1:123456789012:configuration-set/isol8-dev-agent-mail",
    }
    pol = ses.update_reputation_entity_policy.call_args.kwargs
    assert pol["ReputationEntityPolicy"] == "arn:aws:ses:us-east-1:aws:reputation-policy/standard"
    assert pol["ReputationEntityReference"] == "arn:aws:ses:us-east-1:123456789012:tenant/owner-user_1"


@pytest.mark.asyncio
async def test_ensure_tenant_is_idempotent_and_memoized(ses):
    ses.create_tenant.side_effect = ClientError({"Error": {"Code": "AlreadyExistsException"}}, "CreateTenant")
    assert await mod.ensure_tenant("user_1") == "owner-user_1"
    assert await mod.ensure_tenant("user_1") == "owner-user_1"
    assert ses.create_tenant.call_count == 1


@pytest.mark.asyncio
async def test_ensure_tenant_off_when_no_config_set(ses, monkeypatch):
    monkeypatch.setattr(mod.settings, "AGENT_MAIL_CONFIGURATION_SET", "")
    assert await mod.ensure_tenant("user_1") is None
    ses.create_tenant.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_tenant_never_raises(ses):
    ses.create_tenant.side_effect = RuntimeError("boom")
    assert await mod.ensure_tenant("user_1") is None
```

- [ ] **Step 2: run, verify fail.**

- [ ] **Step 3: implement** `apps/backend/core/services/agent_mail_tenant.py`:

```python
"""Per-owner SES tenant — reputation isolation for agent-sent mail.

SES computes bounce/complaint findings per TENANT and (with the managed
``standard`` policy) pauses only that tenant when it goes bad, instead of the
whole account. One tenant per owner, created lazily on the first send, named
deterministically so nothing is stored. Mirrors how per-owner Bedrock AIPs are
created by the backend rather than CDK.

Never raises: a tenant hiccup must not block a send — the send simply goes
out untenanted (still on the configuration set, still observable).
"""

import logging
import re

import anyio
import boto3
from botocore.exceptions import ClientError

from core.config import settings

logger = logging.getLogger(__name__)

_ses_client = None
_sts_account: str | None = None
# ponytail: per-process memo; after a restart the idempotent calls simply run again.
_ensured: set[str] = set()


def _client():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=settings.AWS_REGION)
    return _ses_client


def _account_id() -> str:
    global _sts_account
    if _sts_account is None:
        _sts_account = boto3.client("sts", region_name=settings.AWS_REGION).get_caller_identity()["Account"]
    return _sts_account


def tenant_name_for(owner_id: str) -> str:
    return "owner-" + re.sub(r"[^A-Za-z0-9_-]", "-", owner_id)[:58]


def _identity_domain() -> str:
    """The SES domain identity every env sends under. ``dev.isol8.co`` is a
    mail SUBDOMAIN of the one verified identity ``isol8.co`` (see
    ses-inbound-stack.ts) — SES authorizes subdomain sends via the parent
    identity, so the association target is always the apex."""
    domain = (settings.AGENT_EMAIL_DOMAIN or "").strip().lower()
    return "isol8.co" if domain.endswith("isol8.co") else domain


def _ignore_exists(fn, **kwargs) -> None:
    try:
        fn(**kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "AlreadyExistsException":
            raise


def _ensure_sync(owner_id: str) -> str:
    name = tenant_name_for(owner_id)
    region, account = settings.AWS_REGION, _account_id()
    ses = _client()
    _ignore_exists(ses.create_tenant, TenantName=name)
    for arn in (
        f"arn:aws:ses:{region}:{account}:identity/{_identity_domain()}",
        f"arn:aws:ses:{region}:{account}:configuration-set/{settings.AGENT_MAIL_CONFIGURATION_SET.strip()}",
    ):
        _ignore_exists(ses.create_tenant_resource_association, TenantName=name, ResourceArn=arn)
    ses.update_reputation_entity_policy(
        ReputationEntityType="RESOURCE",
        ReputationEntityReference=f"arn:aws:ses:{region}:{account}:tenant/{name}",
        ReputationEntityPolicy=f"arn:aws:ses:{region}:aws:reputation-policy/standard",
    )
    return name


async def ensure_tenant(owner_id: str) -> str | None:
    if not (settings.AGENT_MAIL_CONFIGURATION_SET or "").strip():
        return None
    name = tenant_name_for(owner_id)
    if name in _ensured:
        return name
    try:
        await anyio.to_thread.run_sync(_ensure_sync, owner_id)
        _ensured.add(name)
        return name
    except Exception:  # noqa: BLE001 — a tenant hiccup must never block a send
        logger.warning("agent_mail_tenant: ensure failed for %s", owner_id, exc_info=True)
        return None
```

- [ ] **Step 4: run, verify pass.**
- [ ] **Step 5: commit.** `git add apps/backend/core/services/agent_mail_tenant.py apps/backend/tests/unit/services/test_agent_mail_tenant.py && git commit -m "feat(agent-mail): per-owner SES tenant"`

---

### Task 6: Address for ANY agent (`ensure_agent_address`) + owner-send from the calling agent

**Files:**
- Modify: `apps/backend/core/services/agent_email.py`, `apps/backend/core/services/agent_mail_owner_send.py`, `apps/backend/core/services/agent_mail_reply.py` (`send_reply` gains `tenant_name`)
- Test: `apps/backend/tests/unit/services/test_agent_email.py` (append), `apps/backend/tests/unit/services/test_agent_mail_owner_send.py` (adjust wiring)

**Interfaces:**
- `agent_email.resolve_company_name(company_id: str) -> str | None` (moved verbatim from `agent_mail_owner_send._resolve_company_name`; owner_send imports it).
- `agent_email.resolve_agent_name(*, agent_id: str, friday_agent_id: str | None) -> str | None` — `"Friday"` when `agent_id == friday_agent_id`; else Paperclip `get_agent(...)["name"]` via `shared_admin_client()` + `get_admin_session_cookie`; None on failure.
- `agent_email.ensure_agent_address(*, owner_id: str, agent_id: str, owner_email: str) -> tuple[str | None, str]` — `(address, "ok")` or `(None, reason)` with reason ∈ `incomplete_provisioning | company_name_unavailable | slug_allocation_failed | agent_name_unavailable | mint_failed`. Fast path: an existing `status == "active"` row for `agent_id` in `agent_email_repo.query_by_owner(owner_id)` → `(address, "ok")` with no Paperclip calls. Otherwise: onboarding row (`PaperclipRepo("paperclip-companies").get_onboarding_state`) → `company_id`, `friday_agent_id`; `resolve_company_name` → `allocate_company_slug` → `resolve_agent_name` → `mint_agent_email_address`. Never raises.
- `agent_mail_owner_send.send_to_owner(*, owner_id, purpose, subject, body_text, body_html=None, agent_id: str | None = None, tenant_name: str | None = None)` — `agent_id` None ⇒ Friday; the mint block is replaced by `ensure_agent_address`; `tenant_name` is passed to `ses_email.send_ses_reply`. `DAILY_SEND_CAP = 50`.
- `agent_mail_reply.send_reply(*, ctx, body_text, body_html=None, tenant_name=None)`.

- [ ] **Step 1: failing tests.** Append to `tests/unit/services/test_agent_email.py`:

```python
import pytest
from unittest.mock import AsyncMock
from core.services import agent_email as ae


@pytest.mark.asyncio
async def test_ensure_agent_address_fast_path_uses_existing_row(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[
        {"agent_id": "agent_x", "status": "active", "address": "atlas@acme.isol8.co"},
    ]))
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(side_effect=AssertionError("must not be called")))
    assert await ae.ensure_agent_address(owner_id="o", agent_id="agent_x", owner_email="o@x.y") == ("atlas@acme.isol8.co", "ok")


@pytest.mark.asyncio
async def test_ensure_agent_address_mints_for_non_friday_agent(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))

    class _Repo:
        def __init__(self, table_name): ...
        async def get_onboarding_state(self, owner_id):
            return {"company_id": "co_1", "friday_agent_id": "agent_friday"}

    monkeypatch.setattr(ae, "PaperclipRepo", _Repo)
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value="Acme Robotics"))
    monkeypatch.setattr(ae, "allocate_company_slug", AsyncMock(return_value="acme-robotics"))
    monkeypatch.setattr(ae, "resolve_agent_name", AsyncMock(return_value="Atlas"))
    mint = AsyncMock(return_value="atlas@acme-robotics.isol8.co")
    monkeypatch.setattr(ae, "mint_agent_email_address", mint)
    out = await ae.ensure_agent_address(owner_id="o", agent_id="agent_atlas", owner_email="o@x.y")
    assert out == ("atlas@acme-robotics.isol8.co", "ok")
    assert mint.call_args.kwargs["agent_name"] == "Atlas"
    assert mint.call_args.kwargs["agent_id"] == "agent_atlas"


@pytest.mark.asyncio
async def test_ensure_agent_address_reports_reason(monkeypatch):
    monkeypatch.setattr(ae.agent_email_repo, "query_by_owner", AsyncMock(return_value=[]))

    class _Repo:
        def __init__(self, table_name): ...
        async def get_onboarding_state(self, owner_id):
            return {"company_id": "co_1", "friday_agent_id": "agent_friday"}

    monkeypatch.setattr(ae, "PaperclipRepo", _Repo)
    monkeypatch.setattr(ae, "resolve_company_name", AsyncMock(return_value=None))
    assert await ae.ensure_agent_address(owner_id="o", agent_id="agent_friday", owner_email="o@x.y") == (None, "company_name_unavailable")


@pytest.mark.asyncio
async def test_resolve_agent_name_friday_short_circuits(monkeypatch):
    assert await ae.resolve_agent_name(agent_id="agent_friday", friday_agent_id="agent_friday") == "Friday"
```

In `test_agent_mail_owner_send.py`, the `wired` fixture patches `mod._resolve_company_name`, `mod.allocate_company_slug`, `mod.mint_agent_email_address` — replace those three with ONE: `monkeypatch.setattr(mod, "ensure_agent_address", _ensure)` where

```python
    async def _ensure(*, owner_id, agent_id, owner_email):
        st.ensure_agent_id = agent_id
        return (st.minted, "ok") if st.minted else (None, st.ensure_reason)
```

(add `ensure_agent_id=None, ensure_reason="mint_failed"` to the SimpleNamespace). Tests that asserted the settle reason `company_name_unavailable` / `slug_allocation_failed` now set `st.minted=None; st.ensure_reason="company_name_unavailable"` and assert the same reason. Add:

```python
@pytest.mark.asyncio
async def test_send_to_owner_uses_calling_agent_for_from_address(wired):
    await mod.send_to_owner(owner_id="user_1", purpose="routine", subject="s", body_text="b", agent_id="agent_atlas")
    assert wired.ensure_agent_id == "agent_atlas"


@pytest.mark.asyncio
async def test_send_to_owner_defaults_to_friday(wired):
    await mod.send_to_owner(owner_id="user_1", purpose="routine", subject="s", body_text="b")
    assert wired.ensure_agent_id == "agent_friday"


def test_daily_cap_is_fifty():
    assert mod.DAILY_SEND_CAP == 50
```

- [ ] **Step 2: run, verify fail.** `uv run pytest tests/unit/services/test_agent_email.py tests/unit/services/test_agent_mail_owner_send.py -q`.

- [ ] **Step 3: implement.** In `agent_email.py` add imports (`from core.repositories.paperclip_repo import PaperclipRepo`, `from core.services.company_identity import allocate_company_slug`, `from core.services.paperclip_admin_client import shared_admin_client`, `from core.services.paperclip_admin_session import get_admin_session_cookie`) and:

```python
async def resolve_company_name(company_id: str) -> str | None:
    """The company's CURRENT name from Paperclip, or None. Read live, never
    cached — the agent renames the company during its first run and that
    rename lands in Paperclip, not in our row."""
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        company = await admin.get_company(session_cookie=cookie, company_id=company_id)
        name = (company or {}).get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:  # noqa: BLE001 — caller maps None to a failed mint
        logger.warning("agent_email: company-name lookup failed for %s: %s", company_id, e)
        return None


async def resolve_agent_name(*, agent_id: str, friday_agent_id: str | None) -> str | None:
    if friday_agent_id and agent_id == friday_agent_id:
        return "Friday"
    try:
        admin = shared_admin_client()
        cookie = await get_admin_session_cookie(admin._http)
        agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
        name = (agent or {}).get("name")
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception as e:  # noqa: BLE001
        logger.warning("agent_email: agent-name lookup failed for %s: %s", agent_id, e)
        return None


async def ensure_agent_address(*, owner_id: str, agent_id: str, owner_email: str) -> tuple[str | None, str]:
    """The active address for ``agent_id`` — minted now if it does not exist.

    Returns ``(address, "ok")`` or ``(None, reason)``. Minting is deferred to
    first use on purpose: the slug derives from the company name the agent
    actually chose (see agent_mail_owner_send's module docstring). Never raises.
    """
    try:
        for row in await agent_email_repo.query_by_owner(owner_id):
            if row.get("agent_id") == agent_id and row.get("status") == "active" and row.get("address"):
                return row["address"], "ok"

        onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
        company_id = onboarding.get("company_id")
        if not company_id:
            return None, "incomplete_provisioning"
        company_name = await resolve_company_name(company_id)
        if not company_name:
            return None, "company_name_unavailable"
        slug = await allocate_company_slug(owner_id=owner_id, name=company_name)
        if not slug:
            return None, "slug_allocation_failed"
        agent_name = await resolve_agent_name(agent_id=agent_id, friday_agent_id=onboarding.get("friday_agent_id"))
        if not agent_name:
            return None, "agent_name_unavailable"
        address = await mint_agent_email_address(
            owner_id=owner_id, agent_id=agent_id, agent_name=agent_name, owner_email=owner_email, slug=slug
        )
        return (address, "ok") if address else (None, "mint_failed")
    except Exception:  # noqa: BLE001
        logger.exception("agent_email: ensure_agent_address failed for %s/%s", owner_id, agent_id)
        return None, "mint_failed"
```

In `agent_mail_owner_send.py`: delete `_resolve_company_name` and the imports it alone needed (`allocate_company_slug`, `mint_agent_email_address`, `shared_admin_client`, `get_admin_session_cookie`); `from core.services.agent_email import ensure_agent_address`; `DAILY_SEND_CAP = 50`; signature gains `agent_id: str | None = None, tenant_name: str | None = None`; replace the block from `if not company_id or not friday_agent_id:` through `if not address: return await _fail("mint_failed", "no_address")` with:

```python
        if not company_id or not friday_agent_id:
            return await _fail("incomplete_provisioning", "error")

        address, reason = await ensure_agent_address(
            owner_id=owner_id, agent_id=agent_id or friday_agent_id, owner_email=owner_email
        )
        if not address:
            result = "no_address" if reason in ("slug_allocation_failed", "mint_failed") else "error"
            return await _fail(reason, result)
```

and pass `tenant_name=tenant_name` to `ses_email.send_ses_reply(...)`. In `agent_mail_reply.send_reply` add `tenant_name: str | None = None` and pass it through to `send_ses_reply`.

- [ ] **Step 4: run, verify pass.** Also `uv run pytest tests/unit/services/test_agent_mail_reply.py tests/unit/services/test_agent_mail_autoreply.py -q`.
- [ ] **Step 5: commit.** `git add apps/backend/core/services/agent_email.py apps/backend/core/services/agent_mail_owner_send.py apps/backend/core/services/agent_mail_reply.py apps/backend/tests/unit/services/test_agent_email.py apps/backend/tests/unit/services/test_agent_mail_owner_send.py && git commit -m "feat(agent-mail): mint addresses for any agent; owner send from the calling agent"`

---

### Task 7: ONE send door (service + policy)

**Files:**
- Create: `apps/backend/core/services/agent_mail_send.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_send.py`

**Interfaces:**
- `async send(*, owner_id: str, agent_id: str, subject: str, body_text: str, body_html: str | None = None, to: str | None = None, reply_key: str | None = None, purpose: str = "routine") -> str`
  Returns: `sent | already_sent | no_address | disabled | rate_limited | forbidden_recipient | outbound_disabled | bad_reply_key | error`.
  - `AGENT_EMAIL_DOMAIN` empty → `disabled`.
  - `tenant = await ensure_tenant(owner_id)` (may be None).
  - neither `to` nor `reply_key` → `send_to_owner(owner_id=…, purpose=purpose, subject, body_text, body_html, agent_id=agent_id, tenant_name=tenant)` (owner path keeps its own cap/first-contact logic).
  - `reply_key` → `parse_reply_key` (None → `bad_reply_key`) → `get_message(address, received_key)` (None or `owner_id` mismatch → `bad_reply_key`) → `row = get_by_address(address)`; `row.get("outbound_disabled")` → `outbound_disabled`; daily cap (`container_repo.try_consume_daily_email_send(owner_id, day=…, cap=DAILY_SEND_CAP)` → `rate_limited`); `send_reply(ctx={owner_id, agent_id: msg["agent_id"], reply_to: msg["from_addr"], in_reply_to: <Message-ID header or "">, references: <References header or "">, subject: msg["subject"]}, body_text, body_html, tenant_name=tenant)` → map `not_sent`→`error`.
  - `to` → `ensure_agent_address(owner_id, agent_id, owner_email)` (owner_email via `lookup_owner_email(org_id=owner_id if is_org_owner_id(owner_id) else None, fallback_user_id=onboarding user_id or owner_id)` — reuse the same lookup the owner path does) → `(None, _)` → `no_address`; `row = get_by_address(address)`; `outbound_disabled` → `outbound_disabled`; `not row.get("send_to_anyone")` → `forbidden_recipient`; cap → `rate_limited`; `ses_email.send_ses_email(from_addr=address, to=to, subject=_env_subject(subject), body_text, body_html, kind="agent_free_send", tenant_name=tenant)` → None → `error`.
  - Every branch `put_metric("agent_email.send", dimensions={"path": "owner|reply|free", "result": <result>})`. Never raises (outer try → `error`).
- `_env_subject` is imported from `agent_mail_owner_send` (it already exists there).

- [ ] **Step 1: failing test** `tests/unit/services/test_agent_mail_send.py`:

```python
"""agent_mail_send.send — the one outbound door. Policy per recipient kind."""

import types
from unittest.mock import AsyncMock

import pytest

from core.services import agent_mail_send as mod

OWNER, AGENT = "user_1", "agent_atlas"


@pytest.fixture
def wired(monkeypatch):
    st = types.SimpleNamespace(
        row={"address": "atlas@acme.isol8.co", "owner_id": OWNER, "agent_id": AGENT, "send_to_anyone": True},
        msg={"owner_id": OWNER, "agent_id": AGENT, "from_addr": "bob@x.y", "subject": "Hi",
             "headers": {"Message-ID": "<m1@x>", "References": "<r0@x>"}},
        cap=True, ses_id="m", owner_result="sent", reply_result="sent",
        owner_kwargs=None, reply_kwargs=None, free_kwargs=None,
    )
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(mod, "ensure_tenant", AsyncMock(return_value="owner-user_1"))

    async def _owner(**kw):
        st.owner_kwargs = kw
        return st.owner_result

    async def _reply(**kw):
        st.reply_kwargs = kw
        return st.reply_result

    async def _free(**kw):
        st.free_kwargs = kw
        return st.ses_id

    monkeypatch.setattr(mod, "send_to_owner", _owner)
    monkeypatch.setattr(mod, "send_reply", _reply)
    monkeypatch.setattr(mod.ses_email, "send_ses_email", _free)
    monkeypatch.setattr(mod, "ensure_agent_address", AsyncMock(return_value=(st.row["address"], "ok")))
    # _owner_email reads the paperclip-companies table — stub the whole helper, not just lookup_owner_email.
    monkeypatch.setattr(mod, "_owner_email", AsyncMock(return_value="owner@x.y"))
    monkeypatch.setattr(mod.agent_email_repo, "get_by_address", AsyncMock(side_effect=lambda a: st.row))
    monkeypatch.setattr(mod, "get_message", AsyncMock(side_effect=lambda a, k: st.msg))
    monkeypatch.setattr(mod.container_repo, "try_consume_daily_email_send", AsyncMock(side_effect=lambda *a, **k: st.cap))
    monkeypatch.setattr(mod, "put_metric", lambda *a, **k: None)
    return st


async def _send(**kw):
    base = dict(owner_id=OWNER, agent_id=AGENT, subject="s", body_text="b")
    base.update(kw)
    return await mod.send(**base)


@pytest.mark.asyncio
async def test_owner_path_delegates_with_agent_and_tenant(wired):
    assert await _send() == "sent"
    assert wired.owner_kwargs["agent_id"] == AGENT
    assert wired.owner_kwargs["tenant_name"] == "owner-user_1"


@pytest.mark.asyncio
async def test_reply_path_threads_from_stored_message(wired):
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "sent"
    ctx = wired.reply_kwargs["ctx"]
    assert ctx["reply_to"] == "bob@x.y" and ctx["in_reply_to"] == "<m1@x>" and ctx["references"] == "<r0@x>"
    assert wired.reply_kwargs["tenant_name"] == "owner-user_1"


@pytest.mark.asyncio
async def test_reply_path_rejects_other_owners_message(wired):
    wired.msg = dict(wired.msg, owner_id="user_2")
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "bad_reply_key"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_bad_key(wired):
    assert await _send(reply_key="garbage") == "bad_reply_key"


@pytest.mark.asyncio
async def test_free_path_requires_send_to_anyone(wired):
    wired.row = dict(wired.row, send_to_anyone=False)
    assert await _send(to="vendor@x.y") == "forbidden_recipient"
    assert wired.free_kwargs is None


@pytest.mark.asyncio
async def test_free_path_blocked_when_outbound_disabled(wired):
    wired.row = dict(wired.row, outbound_disabled="complaint")
    assert await _send(to="vendor@x.y") == "outbound_disabled"


@pytest.mark.asyncio
async def test_free_path_sends_with_tenant_and_cap(wired):
    assert await _send(to="vendor@x.y") == "sent"
    assert wired.free_kwargs["to"] == "vendor@x.y"
    assert wired.free_kwargs["from_addr"] == "atlas@acme.isol8.co"
    assert wired.free_kwargs["tenant_name"] == "owner-user_1"
    assert wired.free_kwargs["kind"] == "agent_free_send"


@pytest.mark.asyncio
async def test_free_path_rate_limited_by_daily_cap(wired):
    wired.cap = False
    assert await _send(to="vendor@x.y") == "rate_limited"


@pytest.mark.asyncio
async def test_reply_path_also_capped(wired):
    wired.cap = False
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "rate_limited"


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(wired, monkeypatch):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _send(to="vendor@x.y") == "disabled"


@pytest.mark.asyncio
async def test_to_and_reply_key_together_is_error(wired):
    assert await _send(to="a@b.c", reply_key="x/y") == "error"
```

- [ ] **Step 2: run, verify fail.**

- [ ] **Step 3: implement** `apps/backend/core/services/agent_mail_send.py`:

```python
"""The ONE outbound door for agent mail.

Three recipient kinds, one policy, evaluated server-side so nothing the agent
passes can widen it:

| recipient                       | allowed when                                   |
|---------------------------------|------------------------------------------------|
| owner (no ``to``/``reply_key``) | always — delegates to ``send_to_owner``        |
| ``reply_key`` (stored inbound)  | row not ``outbound_disabled``; key is owner's   |
| arbitrary ``to``                | row ``send_to_anyone`` and not ``outbound_disabled`` |

All paths share the per-owner durable daily cap and are stamped with the
owner's SES tenant. Never raises — the router returns the result string.
"""

import logging
from datetime import datetime, timezone

from core.auth import is_org_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import agent_email_repo, container_repo
from core.repositories.agent_mail_inbox_repo import get_message, parse_reply_key
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import ses_email
from core.services.agent_email import ensure_agent_address
from core.services.agent_mail_owner_send import DAILY_SEND_CAP, _env_subject, send_to_owner
from core.services.agent_mail_reply import send_reply
from core.services.agent_mail_tenant import ensure_tenant
from core.services.paperclip_owner_email import lookup_owner_email

logger = logging.getLogger(__name__)


def _header(headers: dict, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return v or ""
    return ""


async def _consume_cap(owner_id: str) -> bool:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return await container_repo.try_consume_daily_email_send(owner_id, day=day, cap=DAILY_SEND_CAP)


async def _owner_email(owner_id: str) -> str | None:
    onboarding = await PaperclipRepo(table_name="paperclip-companies").get_onboarding_state(owner_id) or {}
    return await lookup_owner_email(
        org_id=owner_id if is_org_owner_id(owner_id) else None,
        fallback_user_id=onboarding.get("user_id") or owner_id,
    )


async def send(
    *,
    owner_id: str,
    agent_id: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
    to: str | None = None,
    reply_key: str | None = None,
    purpose: str = "routine",
) -> str:
    path = "reply" if reply_key else "free" if to else "owner"
    try:
        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            return _done(path, "disabled")
        if to and reply_key:
            return _done(path, "error")
        tenant = await ensure_tenant(owner_id)

        if path == "owner":
            result = await send_to_owner(
                owner_id=owner_id, purpose=purpose, subject=subject, body_text=body_text,
                body_html=body_html, agent_id=agent_id, tenant_name=tenant,
            )
            return _done(path, result)

        if path == "reply":
            parsed = parse_reply_key(reply_key or "")
            if not parsed:
                return _done(path, "bad_reply_key")
            address, received_key = parsed
            msg = await get_message(address, received_key)
            if not msg or msg.get("owner_id") != owner_id:
                return _done(path, "bad_reply_key")
            row = await agent_email_repo.get_by_address(address) or {}
            if row.get("outbound_disabled"):
                return _done(path, "outbound_disabled")
            if not await _consume_cap(owner_id):
                return _done(path, "rate_limited")
            headers = msg.get("headers") or {}
            result = await send_reply(
                ctx={
                    "owner_id": owner_id,
                    "agent_id": msg.get("agent_id") or agent_id,
                    "reply_to": msg.get("from_addr") or "",
                    "in_reply_to": _header(headers, "Message-ID"),
                    "references": _header(headers, "References"),
                    "subject": msg.get("subject") or "",
                },
                body_text=body_text,
                body_html=body_html,
                tenant_name=tenant,
            )
            return _done(path, "error" if result == "not_sent" else result)

        # free
        owner_email = await _owner_email(owner_id)
        address, _reason = await ensure_agent_address(owner_id=owner_id, agent_id=agent_id, owner_email=owner_email or "")
        if not address:
            return _done(path, "no_address")
        row = await agent_email_repo.get_by_address(address) or {}
        if row.get("outbound_disabled"):
            return _done(path, "outbound_disabled")
        if not row.get("send_to_anyone"):
            return _done(path, "forbidden_recipient")
        if not await _consume_cap(owner_id):
            return _done(path, "rate_limited")
        message_id = await ses_email.send_ses_email(
            from_addr=address, to=to, subject=_env_subject(subject), body_text=body_text,
            body_html=body_html, kind="agent_free_send", tenant_name=tenant,
        )
        return _done(path, "sent" if message_id else "error")
    except Exception:
        logger.exception("agent_mail_send failed owner=%s agent=%s path=%s", owner_id, agent_id, path)
        return _done(path, "error")


def _done(path: str, result: str) -> str:
    put_metric("agent_email.send", dimensions={"path": path, "result": result})
    return result
```

- [ ] **Step 4: run, verify pass.** `uv run pytest tests/unit/services/test_agent_mail_send.py -q`.
- [ ] **Step 5: commit.** `git add apps/backend/core/services/agent_mail_send.py apps/backend/tests/unit/services/test_agent_mail_send.py && git commit -m "feat(agent-mail): one send door with owner/reply/free policy"`

---

### Task 8: Router — container routes + owner settings routes; delete the two old routers

**Files:**
- Create: `apps/backend/routers/agent_mail.py`
- Modify: `apps/backend/main.py` (imports + mounts), `apps/backend/tests/chaos/api/conftest.py`
- Delete: `apps/backend/routers/agent_mail_relay.py`, `apps/backend/routers/agent_mail_send.py`, `apps/backend/tests/unit/routers/test_agent_mail_relay.py`, `apps/backend/tests/unit/routers/test_agent_mail_send.py`
- Test: `apps/backend/tests/unit/routers/test_agent_mail.py`

**Interfaces (mounted at `/api/v1/agent-mail`):**
- `POST /u/{owner_id}/send` — Bearer = owner's `containers.hook_token` (timing-safe). Body `SendRequest{agent_id: str, subject: str, body_text: str, body_html: str|None, to: str|None, reply_key: str|None, purpose: str="routine"}`. 400 when both `to` and `reply_key`; 401 bad token; 429 per-minute burst; else `{"status": <send() result>}`.
- `POST /u/{owner_id}/address` — body `{agent_id}` → `{"address": str|None, "reason": str}` via `ensure_agent_address` (owner email from `agent_mail_send._owner_email`).
- `GET /u/{owner_id}/inbox?agent_id=&since=&limit=20` → `{"address": str|None, "messages": [{key, from, subject, received_at, text}]}` (`text` ≤ 8_000 chars; `key` = `reply_key(address, received_key)`); address resolved with `agent_mail_reply.resolve_from_address(owner_id, agent_id)`; no address → `{"address": None, "messages": []}`.
- `GET /addresses` (Clerk) → `{"addresses": [{address, agent_id, verified_senders, send_to_anyone, outbound_disabled, status}]}` for `resolve_owner_id(auth)`.
- `PATCH /addresses/{agent_id}` (Clerk) body `AddressSettings{verified_senders: list[str]|None, send_to_anyone: bool|None, outbound_disabled: None}` (the only accepted value for `outbound_disabled` is explicit `null` = re-enable; use a sentinel via `model_fields_set`). Entries validated: `x@y.z` or `@y.z`, lowercased; invalid → 400 `{"detail": {"status": "bad_sender", "entry": ...}}`. If no active row for the agent → `ensure_agent_address` first (mint); `(None, reason)` → 409 `{"status": reason}`. Returns the updated row projection.

- [ ] **Step 1: failing tests** `tests/unit/routers/test_agent_mail.py` (mirror `test_agent_mail_send.py`'s style — it is being deleted, copy its auth tests here):

```python
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import agent_mail

OWNER, AGENT, TOKEN = "user_a", "agent_1", "hook-token-for-a"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _reset():
    agent_mail._RATE_BUCKETS_PER_OWNER.clear()
    yield
    agent_mail._RATE_BUCKETS_PER_OWNER.clear()


# `client` comes from tests/conftest.py: TestClient(app) with get_current_user
# overridden to AuthContext(user_id="user_test_123") (personal). Hook-token
# routes ignore that override; the /addresses routes rely on it.


def _container(owner_id=OWNER):
    return {"owner_id": owner_id, "hook_token": TOKEN}


def test_send_missing_bearer_401(client):
    assert client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b"}).status_code == 401


def test_send_wrong_token_401(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b"}, headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401


def test_send_both_to_and_reply_key_400(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "a@b.c", "reply_key": "x/y"}, headers=H)
    assert r.status_code == 400


def test_send_delegates(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())), \
         patch.object(agent_mail, "send", AsyncMock(return_value="sent")) as send:
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/send", json={"agent_id": AGENT, "subject": "s", "body_text": "b", "to": "v@x.y"}, headers=H)
    assert r.status_code == 200 and r.json() == {"status": "sent"}
    assert send.call_args.kwargs["to"] == "v@x.y" and send.call_args.kwargs["agent_id"] == AGENT


def test_inbox_lists_with_keys_and_truncates(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())), \
         patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="a@acme.isol8.co")), \
         patch.object(agent_mail, "list_messages", AsyncMock(return_value=[{"received_key": "2026#m1", "from_addr": "g@github.com", "subject": "Verify", "received_at": "2026", "text": "x" * 9000}])):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}&limit=5", headers=H)
    body = r.json()
    assert body["address"] == "a@acme.isol8.co"
    assert body["messages"][0]["key"] == "a@acme.isol8.co/2026#m1"
    assert len(body["messages"][0]["text"]) == 8000


def test_inbox_without_address_is_empty(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())), \
         patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)):
        r = client.get(f"/api/v1/agent-mail/u/{OWNER}/inbox?agent_id={AGENT}", headers=H)
    assert r.json() == {"address": None, "messages": []}


def test_address_mints(client):
    with patch.object(agent_mail.container_repo, "get_by_owner_id", AsyncMock(return_value=_container())), \
         patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")), \
         patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=("a@acme.isol8.co", "ok"))):
        r = client.post(f"/api/v1/agent-mail/u/{OWNER}/address", json={"agent_id": AGENT}, headers=H)
    assert r.json() == {"address": "a@acme.isol8.co", "reason": "ok"}


def test_validate_sender_entries():
    assert agent_mail._validate_senders(["Bob@X.y", "@ClientCo.com"]) == ["bob@x.y", "@clientco.com"]
    for bad in ["nope", "@", "a@", "@x", "two@@x.y"]:
        with pytest.raises(ValueError):
            agent_mail._validate_senders([bad])


# ---- Clerk routes (owner = user_test_123 via the shared client fixture) ----

def test_list_addresses_projects_active_rows(client):
    rows = [
        {"address": "f@acme.isol8.co", "agent_id": "agent_f", "verified_senders": ["o@x.y"], "send_to_anyone": False, "status": "active"},
        {"address": "old@acme.isol8.co", "agent_id": "agent_f", "status": "alias"},
    ]
    with patch.object(agent_mail.agent_email_repo, "query_by_owner", AsyncMock(return_value=rows)) as q:
        r = client.get("/api/v1/agent-mail/addresses")
    assert r.status_code == 200
    assert q.call_args.args == ("user_test_123",)
    assert r.json() == {"addresses": [{
        "address": "f@acme.isol8.co", "agent_id": "agent_f", "verified_senders": ["o@x.y"],
        "send_to_anyone": False, "outbound_disabled": None, "status": "active",
    }]}


def test_patch_address_lowercases_and_mints_when_missing(client):
    updated = {"address": "f@acme.isol8.co", "agent_id": "agent_f", "verified_senders": ["bob@x.y", "@clientco.com"], "send_to_anyone": True, "status": "active"}
    with patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)), \
         patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")), \
         patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=("f@acme.isol8.co", "ok"))) as mint, \
         patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value=updated)) as upd:
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"verified_senders": ["Bob@X.y", "@ClientCo.com"], "send_to_anyone": True})
    assert r.status_code == 200 and r.json()["send_to_anyone"] is True
    assert mint.call_args.kwargs["agent_id"] == "agent_f" and mint.call_args.kwargs["owner_id"] == "user_test_123"
    assert upd.call_args.args == ("f@acme.isol8.co",)
    assert upd.call_args.kwargs == {"verified_senders": ["bob@x.y", "@clientco.com"], "send_to_anyone": True}


def test_patch_address_bad_sender_400(client):
    with patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="f@acme.isol8.co")):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"verified_senders": ["nope"]})
    assert r.status_code == 400


def test_patch_address_reenable_sends_null(client):
    with patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value="f@acme.isol8.co")), \
         patch.object(agent_mail.agent_email_repo, "update_settings", AsyncMock(return_value={"address": "f@acme.isol8.co"})) as upd:
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"outbound_disabled": None})
    assert r.status_code == 200
    assert upd.call_args.kwargs == {"outbound_disabled": None}


def test_patch_address_mint_failure_409(client):
    with patch.object(agent_mail, "resolve_from_address", AsyncMock(return_value=None)), \
         patch.object(agent_mail, "_owner_email", AsyncMock(return_value="o@x.y")), \
         patch.object(agent_mail, "ensure_agent_address", AsyncMock(return_value=(None, "company_name_unavailable"))):
        r = client.patch("/api/v1/agent-mail/addresses/agent_f", json={"send_to_anyone": True})
    assert r.status_code == 409 and r.json()["detail"]["status"] == "company_name_unavailable"
```

- [ ] **Step 2: run, verify fail.**

- [ ] **Step 3: implement** `apps/backend/routers/agent_mail.py`:

```python
"""Agent mail — container routes (hook-token) + owner settings (Clerk).

Container routes live under ``/u/{owner_id}/…``: the owner is NAMED in the
path and the bearer must timing-safe-match THAT owner's ``containers.hook_token``
(the same model the hooks proxy uses) — a container can only ever act for its
own owner. Policy for WHO a message may go to lives in
``core/services/agent_mail_send.send``; this router only authenticates, rate
limits, validates shapes, and relays the result string.
"""

import hmac
import logging
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import agent_email_repo, container_repo
from core.repositories.agent_mail_inbox_repo import list_messages, reply_key
from core.services.agent_email import ensure_agent_address
from core.services.agent_mail_reply import resolve_from_address
from core.services.agent_mail_send import _owner_email, send

logger = logging.getLogger(__name__)
router = APIRouter()

RATE_LIMIT_PER_MINUTE = 60
_RATE_BUCKETS_PER_OWNER: dict[str, deque[float]] = defaultdict(deque)
_INBOX_TEXT_CHARS = 8_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^@[^@\s]+\.[^@\s]+$")


def _check_rate_limit(owner_id: str) -> bool:
    now = time.monotonic()
    q = _RATE_BUCKETS_PER_OWNER[owner_id]
    while q and q[0] < now - 60.0:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


async def _require_hook_token(owner_id: str, request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_token")
    presented = auth[len("Bearer ") :]
    container = await container_repo.get_by_owner_id(owner_id)
    expected = container.get("hook_token") if container else ""
    if not expected or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail={"status": "invalid_token"})
    if not _check_rate_limit(owner_id):
        raise HTTPException(status_code=429, detail={"status": "rate_limited"})


class SendRequest(BaseModel):
    agent_id: str
    subject: str
    body_text: str
    body_html: str | None = None
    to: str | None = None
    reply_key: str | None = None
    purpose: str = "routine"


@router.post("/u/{owner_id}/send")
async def send_mail(owner_id: str, body: SendRequest, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    if body.to and body.reply_key:
        raise HTTPException(status_code=400, detail={"status": "to_and_reply_key"})
    result = await send(
        owner_id=owner_id, agent_id=body.agent_id, subject=body.subject, body_text=body.body_text,
        body_html=body.body_html, to=body.to, reply_key=body.reply_key, purpose=body.purpose,
    )
    return {"status": result}


class AddressRequest(BaseModel):
    agent_id: str


@router.post("/u/{owner_id}/address")
async def mint_address(owner_id: str, body: AddressRequest, request: Request) -> dict:
    await _require_hook_token(owner_id, request)
    owner_email = await _owner_email(owner_id)
    address, reason = await ensure_agent_address(owner_id=owner_id, agent_id=body.agent_id, owner_email=owner_email or "")
    return {"address": address, "reason": reason}


@router.get("/u/{owner_id}/inbox")
async def inbox(owner_id: str, agent_id: str, request: Request, since: str | None = None, limit: int = 20) -> dict:
    await _require_hook_token(owner_id, request)
    address = await resolve_from_address(owner_id, agent_id)
    if not address:
        return {"address": None, "messages": []}
    rows = await list_messages(address, since=since, limit=limit)
    return {
        "address": address,
        "messages": [
            {
                "key": reply_key(address, r["received_key"]),
                "from": r.get("from_addr", ""),
                "subject": r.get("subject", ""),
                "received_at": r.get("received_at", ""),
                "text": (r.get("text") or "")[:_INBOX_TEXT_CHARS],
            }
            for r in rows
        ],
    }


# ---- owner settings (Clerk) -------------------------------------------------

def _project(row: dict) -> dict:
    return {
        "address": row.get("address"),
        "agent_id": row.get("agent_id"),
        "verified_senders": row.get("verified_senders") or [],
        "send_to_anyone": bool(row.get("send_to_anyone")),
        "outbound_disabled": row.get("outbound_disabled"),
        "status": row.get("status"),
    }


def _validate_senders(entries: list[str]) -> list[str]:
    out: list[str] = []
    for raw in entries:
        e = (raw or "").strip().lower()
        if not (_EMAIL_RE.match(e) or _DOMAIN_RE.match(e)):
            raise ValueError(e)
        out.append(e)
    return out


@router.get("/addresses")
async def list_addresses(auth: AuthContext = Depends(get_current_user)) -> dict:
    rows = await agent_email_repo.query_by_owner(resolve_owner_id(auth))
    return {"addresses": [_project(r) for r in rows if r.get("status") == "active"]}


class AddressSettings(BaseModel):
    verified_senders: list[str] | None = None
    send_to_anyone: bool | None = None
    outbound_disabled: str | None = None  # only explicit null is honoured (re-enable)


@router.patch("/addresses/{agent_id}")
async def patch_address(agent_id: str, body: AddressSettings, auth: AuthContext = Depends(get_current_user)) -> dict:
    owner_id = resolve_owner_id(auth)
    address = await resolve_from_address(owner_id, agent_id)
    if not address:
        owner_email = await _owner_email(owner_id)
        address, reason = await ensure_agent_address(owner_id=owner_id, agent_id=agent_id, owner_email=owner_email or "")
        if not address:
            raise HTTPException(status_code=409, detail={"status": reason})
    kwargs: dict = {}
    if body.verified_senders is not None:
        try:
            kwargs["verified_senders"] = _validate_senders(body.verified_senders)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"status": "bad_sender", "entry": str(e)}) from e
    if body.send_to_anyone is not None:
        kwargs["send_to_anyone"] = body.send_to_anyone
    if "outbound_disabled" in body.model_fields_set and body.outbound_disabled is None:
        kwargs["outbound_disabled"] = None
    row = await agent_email_repo.update_settings(address, **kwargs)
    if row is None:
        raise HTTPException(status_code=404, detail={"status": "no_address"})
    return _project(row)
```

`main.py`: replace `agent_mail_relay, agent_mail_send,` in the import list with `agent_mail,`; replace the two `include_router` blocks (L898-913) with:

```python
# Agent mail (container routes under /u/{owner_id}/… use hook-token auth;
# /addresses routes use Clerk). Policy lives in core/services/agent_mail_send.
app.include_router(agent_mail.router, prefix="/api/v1/agent-mail", tags=["agent-mail"])
```

`git rm` the two old routers and their two tests. Chaos `conftest.py`: delete the `send-to-owner` entry (and its comment block) and add:

```python
    # Agent-mail container routes: hook-token lookup on the DDB containers
    # table before anything else can run → unmocked GetItem 5xx. Covered by
    # tests/unit/routers/test_agent_mail.py.
    "POST /api/v1/agent-mail/u/{owner_id}/send": "hook-token lookup; containers table not provisioned hermetically",
    "POST /api/v1/agent-mail/u/{owner_id}/address": "hook-token lookup; containers table not provisioned hermetically",
    "GET /api/v1/agent-mail/u/{owner_id}/inbox": "hook-token lookup; containers table not provisioned hermetically",
```

- [ ] **Step 4: run, verify pass.** `uv run pytest tests/unit/routers/test_agent_mail.py -q`, then `uv run pytest tests/chaos -q` (the negative pass must not flag the new routes), then `grep -rn "agent_mail_relay\|routers/agent_mail_send\|routers import.*agent_mail_send\|send-to-owner\|agent_mail_token\|reply_token" apps/backend --include='*.py'` → zero hits (the new service module `core/services/agent_mail_send.py` is fine; what must be gone is every reference to the deleted ROUTERS, the old route path, the token module, and `reply_token`). Docstrings in `agent_mail_owner_send.py` / `agent_mail_reply.py` that still describe "the relay" → reword to name `agent_mail_send.send`.

- [ ] **Step 5: commit.**
```bash
git add -u apps/backend/main.py apps/backend/routers apps/backend/tests/unit/routers apps/backend/tests/chaos/api/conftest.py apps/backend/core/services/agent_mail_owner_send.py apps/backend/core/services/agent_mail_reply.py
git add apps/backend/routers/agent_mail.py apps/backend/tests/unit/routers/test_agent_mail.py
git commit -m "feat(agent-mail): one router; delete reply relay + owner-send routes"
```

---

### Task 9: Complaint feedback — `POST /webhooks/ses-events`

**Files:**
- Modify: `apps/backend/routers/ses_inbound.py`
- Test: `apps/backend/tests/unit/routers/test_ses_inbound.py` (append)

**Interfaces:**
- `POST /api/v1/webhooks/ses-events` — header `X-Isol8-SES-Secret` (same secret as ses-inbound; 401 otherwise; `{"status":"disabled"}` when unset). Body = the EventBridge event as delivered by the API destination: `{"detail-type": "Email Complaint Received", "source": "aws.ses", "detail": {"eventType": "Complaint", "mail": {"source": "friday@acme.isol8.co", ...}, "complaint": {...}}}`. Non-complaint → `{"status":"ignored"}`. `mail.source` → `normalize` → `get_by_address`; unknown → `{"status":"unknown_source"}`. Else: every active row of that owner → `update_settings(address, outbound_disabled="complaint")`; then `send_to_owner(owner_id=…, purpose="routine", subject="Outbound email paused", body_text=…)` best-effort; → `{"status":"disabled","owner_id":…,"addresses":N}`.

- [ ] **Step 1: failing test** (append to `test_ses_inbound.py`):

```python
def _complaint(source="friday@acme-robotics.isol8.co", event_type="Complaint"):
    return {"detail-type": "Email Complaint Received", "source": "aws.ses",
            "detail": {"eventType": event_type, "mail": {"source": source}, "complaint": {"complaintFeedbackType": "abuse"}}}


def test_ses_events_complaint_disables_owner_outbound(client, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(ses_inbound, "get_by_address", AsyncMock(return_value=_row()))
    monkeypatch.setattr(ses_inbound, "query_by_owner", AsyncMock(return_value=[_row(), dict(_row(), address="atlas@acme-robotics.isol8.co")]))
    upd = AsyncMock(return_value={})
    monkeypatch.setattr(ses_inbound, "update_settings", upd)
    notify = AsyncMock(return_value="sent")
    monkeypatch.setattr(ses_inbound, "send_to_owner", notify)
    r = client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Isol8-SES-Secret": SECRET})
    assert r.status_code == 200 and r.json()["status"] == "disabled" and r.json()["addresses"] == 2
    assert {c.args[0] for c in upd.call_args_list} == {"friday@acme-robotics.isol8.co", "atlas@acme-robotics.isol8.co"}
    assert all(c.kwargs == {"outbound_disabled": "complaint"} for c in upd.call_args_list)
    assert notify.call_args.kwargs["owner_id"] == OWNER_ID


def test_ses_events_ignores_non_complaint(client, monkeypatch):
    _enable(monkeypatch)
    r = client.post("/api/v1/webhooks/ses-events", json=_complaint(event_type="Delivery"), headers={"X-Isol8-SES-Secret": SECRET})
    assert r.json()["status"] == "ignored"


def test_ses_events_bad_secret_401(client, monkeypatch):
    _enable(monkeypatch)
    assert client.post("/api/v1/webhooks/ses-events", json=_complaint(), headers={"X-Isol8-SES-Secret": "nope"}).status_code == 401
```

- [ ] **Step 2: run, verify fail.**
- [ ] **Step 3: implement** in `ses_inbound.py` (imports: `from core.repositories.agent_email_repo import get_by_address, normalize, query_by_owner, update_settings`, `from core.services.agent_mail_owner_send import send_to_owner`):

```python
_PAUSED_SUBJECT = "Outbound email paused"
_PAUSED_BODY = (
    "A recipient marked an email from one of your agents as spam, so outbound email "
    "from your agents is paused. Replies to your own messages still work.\n\n"
    "Review who your agents may email and re-enable sending in Control → Agents → Email."
)


@router.post("/ses-events", include_in_schema=False)
async def handle_ses_events(request: Request):
    """SES complaint events (configuration-set → EventBridge → API destination)."""
    if not settings.SES_INBOUND_SECRET or not settings.AGENT_EMAIL_DOMAIN.strip():
        return {"status": "disabled"}
    presented = request.headers.get("X-Isol8-SES-Secret", "")
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), settings.SES_INBOUND_SECRET.encode("utf-8")):
        return JSONResponse(status_code=401, content={"status": "bad_secret"})
    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"status": "bad_json"})
    detail = (event or {}).get("detail") or {}
    if (detail.get("eventType") or "").lower() != "complaint":
        return {"status": "ignored"}
    source = (detail.get("mail") or {}).get("source") or ""
    row = await get_by_address(normalize(source)) if source else None
    if not row:
        return {"status": "unknown_source"}
    owner_id = row["owner_id"]
    rows = [r for r in await query_by_owner(owner_id) if r.get("status") == "active"]
    for r in rows:
        await update_settings(r["address"], outbound_disabled="complaint")
    put_metric("agent_email.complaint", dimensions={"result": "disabled"})
    try:
        await send_to_owner(owner_id=owner_id, purpose="routine", subject=_PAUSED_SUBJECT, body_text=_PAUSED_BODY)
    except Exception:  # noqa: BLE001 — notification is best-effort
        logger.warning("ses_events: owner notify failed for %s", owner_id, exc_info=True)
    return {"status": "disabled", "owner_id": owner_id, "addresses": len(rows)}
```

- [ ] **Step 4: run, verify pass.** `uv run pytest tests/unit/routers/test_ses_inbound.py -q`.
- [ ] **Step 5: commit.** `git add apps/backend/routers/ses_inbound.py apps/backend/tests/unit/routers/test_ses_inbound.py && git commit -m "feat(agent-mail): SES complaint disables owner outbound"`

---

### Task 10: CDK — outbound stack (config set, complaint → API destination), IAM, env

**Files:**
- Create: `apps/infra/lib/stacks/agent-mail-outbound-stack.ts`
- Modify: `apps/infra/lib/isol8-stage.ts`, `apps/infra/lib/stacks/service-stack.ts`

**Interfaces:**
- `AgentMailOutboundStackProps { envName: string; apiDomain: string; sesIngestSecretName: string }`; exposes `configurationSetName: string` (literal `isol8-${envName}-agent-mail`).
- ServiceStack prop `agentMailConfigurationSet?: string` → env `AGENT_MAIL_CONFIGURATION_SET`.

- [ ] **Step 1: write the stack** `apps/infra/lib/stacks/agent-mail-outbound-stack.ts`:

```ts
import * as cdk from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as ses from "aws-cdk-lib/aws-ses";
import { Construct } from "constructs";

export interface AgentMailOutboundStackProps extends cdk.StackProps {
  /** Stage env name ("dev" | "prod"); names the configuration set. */
  envName: string;
  /** Public REST host, e.g. "api-dev.isol8.co" / "api.isol8.co". */
  apiDomain: string;
  /** By-name import of THIS env's ses_ingest_secret — the backend's /webhooks/ses-events checks it. */
  sesIngestSecretName: string;
}

/**
 * Outbound agent-mail plumbing, per env:
 *   configuration set (every agent send is stamped with it — core/services/ses_email.py)
 *     → COMPLAINT events → EventBridge default bus
 *     → rule filtered on this env's configuration-set tag
 *     → API destination → POST https://{apiDomain}/api/v1/webhooks/ses-events
 * Per-owner SES TENANTS are created by the backend at first send
 * (core/services/agent_mail_tenant.py) — dynamic per owner, so not declared here.
 */
export class AgentMailOutboundStack extends cdk.Stack {
  public readonly configurationSetName: string;

  constructor(scope: Construct, id: string, props: AgentMailOutboundStackProps) {
    super(scope, id, props);
    const { envName, apiDomain, sesIngestSecretName } = props;

    this.configurationSetName = `isol8-${envName}-agent-mail`;
    const configSet = new ses.CfnConfigurationSet(this, "AgentMailConfigSet", {
      name: this.configurationSetName,
    });

    const eventDestination = new ses.CfnConfigurationSetEventDestination(this, "ComplaintsToEventBridge", {
      configurationSetName: this.configurationSetName,
      eventDestination: {
        name: "complaints-eventbridge",
        enabled: true,
        matchingEventTypes: ["COMPLAINT"],
        eventBridgeDestination: {
          eventBusArn: `arn:aws:events:${this.region}:${this.account}:event-bus/default`,
        },
      },
    });
    eventDestination.addDependency(configSet);

    const connection = new events.Connection(this, "BackendConnection", {
      // Same shared secret the SES-ingest Lambda presents — the backend's
      // ses-events handler verifies it with the same compare.
      authorization: events.Authorization.apiKey(
        "X-Isol8-SES-Secret",
        cdk.SecretValue.secretsManager(sesIngestSecretName),
      ),
      description: `isol8 ${envName} agent-mail SES events → backend`,
    });
    const destination = new events.ApiDestination(this, "BackendSesEvents", {
      connection,
      endpoint: `https://${apiDomain}/api/v1/webhooks/ses-events`,
      httpMethod: events.HttpMethod.POST,
    });
    new events.Rule(this, "SesComplaintRule", {
      description: `isol8 ${envName}: SES complaints on ${this.configurationSetName}`,
      eventPattern: {
        source: ["aws.ses"],
        detailType: ["Email Complaint Received"],
        // dev + prod share one account and one default bus — filter on the
        // configuration-set tag SES stamps on every event.
        detail: { mail: { tags: { "ses:configuration-set": [this.configurationSetName] } } },
      },
      targets: [new targets.ApiDestination(destination)],
    });

    cdk.Tags.of(this).add("Project", "isol8");
    cdk.Tags.of(this).add("Environment", envName);
  }
}
```

- [ ] **Step 2: wire the stage.** In `isol8-stage.ts`, after the `sesInbound` block and before `NetworkStack` (imports at top: `import { AgentMailOutboundStack } from "./stacks/agent-mail-outbound-stack";`):

```ts
    // Outbound agent-mail plumbing exists wherever agent email is on (dev + prod).
    const agentEmailDomain = env === "prod" ? "isol8.co" : env === "dev" ? "dev.isol8.co" : "";
    let agentMailOutbound: AgentMailOutboundStack | undefined;
    if (agentEmailDomain) {
      agentMailOutbound = new AgentMailOutboundStack(this, `isol8-${env}-agent-mail-outbound`, {
        stackName: `isol8-${env}-agent-mail-outbound`,
        envName: env,
        apiDomain: env === "prod" ? "api.isol8.co" : `api-${env}.isol8.co`,
        sesIngestSecretName: `isol8/${env}/ses_ingest_secret`,
      });
      agentMailOutbound.addDependency(auth);
    }
```

and in the `ServiceStack` props replace the inline `agentEmailDomain:` ternary with `agentEmailDomain,` and add `agentMailConfigurationSet: agentMailOutbound?.configurationSetName,`.

- [ ] **Step 3: service-stack.** Add prop `agentMailConfigurationSet?: string;` (doc: "SES configuration set every agent-mail send is stamped with; empty ⇒ unstamped"). Env: after `AGENT_EMAIL_ATTACHMENTS_BUCKET` add `AGENT_MAIL_CONFIGURATION_SET: props.agentMailConfigurationSet ?? "",`. In the `BackendSesSend` managed policy add a second statement:

```ts
        new iam.PolicyStatement({
          sid: "AgentMailTenants",
          actions: [
            "ses:CreateTenant",
            "ses:GetTenant",
            "ses:CreateTenantResourceAssociation",
            "ses:UpdateReputationEntityPolicy",
            "ses:GetReputationEntity",
            "ses:SendEmail",
            "ses:SendRawEmail",
          ],
          resources: [
            `arn:aws:ses:${this.region}:${this.account}:tenant/*`,
            `arn:aws:ses:${this.region}:${this.account}:configuration-set/isol8-${env}-agent-mail`,
            `arn:aws:ses:${this.region}:${this.account}:identity/isol8.co`,
          ],
        }),
```

- [ ] **Step 4: typecheck + synth.** `cd apps/infra && pnpm exec tsc --noEmit && npx cdk synth isol8-dev/isol8-dev-agent-mail-outbound --quiet` (stack id form: check `npx cdk ls | grep agent-mail`). If `apps/infra/test` exists run `pnpm exec jest`.
- [ ] **Step 5: commit.** `git add apps/infra/lib/stacks/agent-mail-outbound-stack.ts apps/infra/lib/isol8-stage.ts apps/infra/lib/stacks/service-stack.ts && git commit -m "feat(agent-mail): SES configuration set, complaint → API destination, tenant IAM"`

---

### Task 11: Skill — one `isol8-mail.sh` + SKILL.md

**Files:**
- Rewrite: `apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh`
- Delete: `apps/infra/openclaw/skills/isol8/scripts/isol8-owner-email.sh`
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md` (maintenance anchors L44-45; sections "Your email address", "Emailing your owner", "Replying by email" L203-297)

**Interfaces:** CLI
```
isol8-mail.sh address                         # prints your address (mints it if needed)
isol8-mail.sh inbox [--since ISO] [--limit N] # newest first: key / from / subject / date / text
isol8-mail.sh send --owner --subject S        # body on stdin
isol8-mail.sh send --to ADDR --subject S      # body on stdin (owner must have allowed it)
isol8-mail.sh send --reply KEY                # body on stdin; subject comes from the thread
Options: --as AGENT_ID (default: $PAPERCLIP_AGENT_ID, else the workspace-<id> cwd)
```

- [ ] **Step 1: write the script**:

```bash
#!/usr/bin/env bash
# Isol8 agent-mail CLI — your address, your inbox, and the one way to send.
# All policy (who you may email) is decided by the backend; this only relays.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  isol8-mail.sh address
  isol8-mail.sh inbox [--since ISO8601] [--limit N]
  isol8-mail.sh send --owner --subject "S"            (body on stdin)
  isol8-mail.sh send --to someone@example.com --subject "S"
  isol8-mail.sh send --reply <Reply-Key>               (body on stdin)
Options:
  --as <agent_id>   act as this agent (default: $PAPERCLIP_AGENT_ID, else your workspace-<id> cwd)
Env (already set): OPENCLAW_HOOK_TOKEN, ISOL8_API_URL, ISOL8_OWNER_ID
EOF
}
die() { printf '%s\n' "$1" >&2; exit 1; }

[[ $# -ge 1 ]] || { usage >&2; exit 2; }
cmd="$1"; shift
command -v jq >/dev/null || die 'Missing jq'; command -v curl >/dev/null || die 'Missing curl'
[[ -n "${OPENCLAW_HOOK_TOKEN:-}" ]] || die 'Missing OPENCLAW_HOOK_TOKEN.'
[[ -n "${ISOL8_API_URL:-}" ]] || die 'Missing ISOL8_API_URL.'
[[ -n "${ISOL8_OWNER_ID:-}" ]] || die 'Missing ISOL8_OWNER_ID.'

agent_id="${PAPERCLIP_AGENT_ID:-}"
case "$PWD" in */workspace-*) [[ -n "$agent_id" ]] || agent_id="${PWD##*/workspace-}" ;; esac
to=""; subject=""; reply=""; owner=false; since=""; limit="20"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --as) agent_id="${2:-}"; shift 2 ;;
    --to) to="${2:-}"; shift 2 ;;
    --subject) subject="${2:-}"; shift 2 ;;
    --reply) reply="${2:-}"; shift 2 ;;
    --owner) owner=true; shift ;;
    --since) since="${2:-}"; shift 2 ;;
    --limit) limit="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -n "$agent_id" ]] || die 'Cannot tell which agent you are: pass --as <agent_id>.'

base="${ISOL8_API_URL%/}/api/v1/agent-mail/u/${ISOL8_OWNER_ID}"
auth=(-H "Authorization: Bearer ${OPENCLAW_HOOK_TOKEN}" -H 'Content-Type: application/json')

call() { # method path [json]
  local out code
  out="$(curl -sS -w $'\n%{http_code}' -X "$1" "${auth[@]}" ${3:+-d "$3"} "$2")"
  code="${out##*$'\n'}"; out="${out%$'\n'*}"
  [[ "$code" -ge 200 && "$code" -lt 300 ]] || { printf 'HTTP %s: %s\n' "$code" "$out" >&2; exit 1; }
  printf '%s' "$out"
}

case "$cmd" in
  address)
    res="$(call POST "$base/address" "$(jq -nc --arg a "$agent_id" '{agent_id:$a}')")"
    addr="$(jq -r '.address // empty' <<<"$res")"
    [[ -n "$addr" ]] || die "No address yet ($(jq -r '.reason' <<<"$res")). Name your company first, then retry."
    printf '%s\n' "$addr" ;;
  inbox)
    q="agent_id=$(jq -rn --arg a "$agent_id" '$a|@uri')&limit=${limit}"
    [[ -n "$since" ]] && q="$q&since=$(jq -rn --arg s "$since" '$s|@uri')"
    res="$(call GET "$base/inbox?$q")"
    jq -r 'if (.messages|length)==0 then "Inbox empty (address: \(.address // "none yet"))."
           else .messages[] | "=== \(.received_at)  from: \(.from)\nsubject: \(.subject)\nReply-Key: \(.key)\n\(.text)\n" end' <<<"$res" ;;
  send)
    body="$(cat)"; [[ -n "$body" ]] || die 'Empty body: pipe the email text on stdin.'
    n=0; $owner && n=$((n+1)); [[ -n "$to" ]] && n=$((n+1)); [[ -n "$reply" ]] && n=$((n+1))
    [[ $n -eq 1 ]] || die 'Pick exactly one of --owner, --to, --reply.'
    [[ -n "$reply" || -n "$subject" ]] || die '--subject is required.'
    payload="$(jq -nc --arg a "$agent_id" --arg s "$subject" --arg b "$body" --arg t "$to" --arg r "$reply" \
      '{agent_id:$a, subject:$s, body_text:$b} + (if $t != "" then {to:$t} else {} end) + (if $r != "" then {reply_key:$r} else {} end)')"
    res="$(call POST "$base/send" "$payload")"
    status="$(jq -r '.status // "unknown"' <<<"$res")"
    case "$status" in
      sent|already_sent) echo "Email $status." ;;
      forbidden_recipient) die 'Not sent: your owner has not allowed you to email people other than them. Ask them to enable it in Control → Agents → Email.' ;;
      outbound_disabled) die 'Not sent: outbound email is paused for this account (a recipient reported spam). Your owner can re-enable it in Control → Agents → Email.' ;;
      rate_limited) die 'Not sent: daily email limit reached. Try tomorrow.' ;;
      no_address) die 'Not sent: you have no address yet — name your company, then run: isol8-mail.sh address' ;;
      bad_reply_key) die 'Not sent: that Reply-Key is not one of your messages.' ;;
      *) die "Not sent (status: $status)." ;;
    esac ;;
  *) usage >&2; exit 2 ;;
esac
```

`chmod +x`; `git rm apps/infra/openclaw/skills/isol8/scripts/isol8-owner-email.sh`. Sanity: `bash -n isol8-mail.sh`.

- [ ] **Step 2: SKILL.md.** Anchors L44-45 become one line: `  - Email ................... /app/skills/isol8/scripts/isol8-mail.sh + backend routers/agent_mail.py + core/services/agent_mail_send.py (one send door; policy table in its docstring)`. Replace "Your email address" / "Emailing your owner" / "Replying by email" with:

```markdown
## Your email

You have (or can have) your own address: `<you>@<company-slug>.isol8.co`. It is
minted the first time you ask for it, from the company's CURRENT name — so name
the company first (see the Paperclip section), then:

```
/app/skills/isol8/scripts/isol8-mail.sh address
```

(absolute path — the skill lives at /app/skills/isol8; your cwd is your workspace.)
If `isol8-facts.json` says `email_enabled: false`, email is off here — do not
offer it.

**Inbox.** Mail to your address is stored for 30 days. Some of it also wakes
you as a chat message (see below); the rest you read on demand:

```
/app/skills/isol8/scripts/isol8-mail.sh inbox --limit 5
```

**Getting a verification code.** When you sign up for a service with your
address, the confirmation email does NOT wake you — it is a stranger's mail.
Run `inbox` a few seconds after requesting the code and read it from there.
Re-run if it has not arrived yet (mail takes up to a minute).

**Who can wake you.** Your owner keeps a list of senders/domains
(Control → Agents → Email). Mail from them, or from your owner, arrives as a
chat message starting with `From:` / `Subject:` / `Reply-Key:`. Everything else
is inbox-only.

**Sending — one command, three recipients:**

```
printf '%s' "<body>" | /app/skills/isol8/scripts/isol8-mail.sh send --owner --subject "<subject>"
printf '%s' "<body>" | /app/skills/isol8/scripts/isol8-mail.sh send --reply "<Reply-Key>"
printf '%s' "<body>" | /app/skills/isol8/scripts/isol8-mail.sh send --to someone@example.com --subject "<subject>"
```

- `--owner` always works. `--reply` answers whoever sent that message (the
  key is in its header block; never paste it into the body). `--to` anyone
  else works only if your owner enabled it — otherwise the command tells you
  so; ask the owner, don't retry.
- There is a daily limit. Send when you have something worth reading.
- If a recipient reports spam, outbound is paused for the whole account until
  the owner re-enables it. Never email people who did not ask to hear from you.
```

- [ ] **Step 3: lint.** `bash -n apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh`; `grep -rn "isol8-owner-email\|Reply-Token\|reply-token" apps/infra/openclaw apps/backend --include='*.md' --include='*.py' --include='*.sh'` → zero hits outside git history (fix any stragglers, e.g. `onboarding_brief.py` / `friday_identity.py` prompts that mention the old script: `grep -rn "isol8-owner-email" apps/backend/core/services`).
- [ ] **Step 4: commit.** `git add -u apps/infra/openclaw/skills/isol8 && git add apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh && git commit -m "feat(agent-mail): one isol8-mail CLI (address/inbox/send) + skill text"`

---

### Task 12: Frontend — Email tab

**Files:**
- Create: `apps/frontend/src/components/control/panels/AgentEmailSection.tsx`
- Modify: `apps/frontend/src/components/control/panels/AgentsPanel.tsx` (tabs array ~L214-220, render ~L245; lucide import add `Mail`)

**Interfaces:** `AgentEmailSection({ agentId })` — SWR `GET /agent-mail/addresses`; row for `agentId`; editor saves via `PATCH /agent-mail/addresses/{agentId}`.

- [ ] **Step 1: component** (native checkbox + textarea; one-per-line senders):

```tsx
"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import { Mail } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

interface AddressRow {
  address: string;
  agent_id: string;
  verified_senders: string[];
  send_to_anyone: boolean;
  outbound_disabled: string | null;
}

export function AgentEmailSection({ agentId }: { agentId: string }) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<{ addresses: AddressRow[] }>(
    "/agent-mail/addresses",
    () => api.get("/agent-mail/addresses") as Promise<{ addresses: AddressRow[] }>,
    { revalidateOnFocus: false },
  );
  const row = data?.addresses.find((a) => a.agent_id === agentId);
  const [senders, setSenders] = useState("");
  const [anyone, setAnyone] = useState(false);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setSenders((row?.verified_senders ?? []).join("\n"));
    setAnyone(row?.send_to_anyone ?? false);
  }, [row?.address, row?.verified_senders, row?.send_to_anyone]);

  if (error && !data) return <div className="p-4 text-sm text-[#dc2626]">Failed to load email settings.</div>;
  if (isLoading || !data) return <div className="p-4 text-sm text-[var(--mute)]">Loading email…</div>;

  const save = async (extra: Record<string, unknown> = {}) => {
    setSaving(true);
    setErr(null);
    try {
      await api.patch(`/agent-mail/addresses/${agentId}`, {
        verified_senders: senders.split("\n").map((s) => s.trim()).filter(Boolean),
        send_to_anyone: anyone,
        ...extra,
      });
      await mutate();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4 p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><Mail className="h-4 w-4" /> Email</h3>
      <div className="rounded-md border border-[var(--edge)] p-3 space-y-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-[var(--mute)]">Address</div>
          {row ? (
            <code className="text-sm">{row.address}</code>
          ) : (
            <p className="text-xs text-[var(--mute)]">Not created yet — it appears the first time this agent uses email, or when you save below.</p>
          )}
        </div>
        {row?.outbound_disabled && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs">
            Outbound email is paused: a recipient reported a message as spam.
            <Button variant="outline" size="sm" className="ml-2" disabled={saving} onClick={() => save({ outbound_disabled: null })}>
              Re-enable
            </Button>
          </div>
        )}
        <label className="block text-xs">
          <span className="font-semibold">Who can wake this agent by email</span>
          <span className="block text-[var(--mute)]">One per line: an address (bob@example.com) or a domain (@example.com). You are always allowed.</span>
          <textarea
            className="mt-1 w-full rounded-md border border-[var(--edge)] bg-transparent p-2 font-mono text-xs"
            rows={4}
            value={senders}
            onChange={(e) => setSenders(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input type="checkbox" checked={anyone} onChange={(e) => setAnyone(e.target.checked)} />
          Let this agent email anyone (daily limit applies; a spam report pauses all outbound)
        </label>
        {err && <p className="text-xs text-[#dc2626]">{err}</p>}
        <Button size="sm" disabled={saving} onClick={() => save()}>{saving ? "Saving…" : "Save"}</Button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: tab.** In `AgentsPanel.tsx` add `Mail` to the lucide import, `import { AgentEmailSection } from "./AgentEmailSection";`, append `{ id: "email", icon: Mail, label: "Email" },` to the tabs array (after tools, before the channels spread — it is NOT gated on subscription: free-tier owners have agent email too), and render `{activeTab === "email" && <AgentEmailSection agentId={selected.id} />}`. If `activeTab`'s type is a union literal, add `"email"`.
- [ ] **Step 3: typecheck + lint + render self-review.** `cd apps/frontend && pnpm exec tsc --noEmit && pnpm exec eslint src/components/control/panels/AgentEmailSection.tsx src/components/control/panels/AgentsPanel.tsx`. Then run the app against dev (`pnpm dev`, sign in, Control → Agents → pick Friday → Email tab) and confirm: address line, textarea, checkbox, Save round-trips (`PATCH` 200, values persist on reload). Screenshot to the scratchpad dir for the PR.
- [ ] **Step 4: commit.** `git add apps/frontend/src/components/control/panels/AgentEmailSection.tsx apps/frontend/src/components/control/panels/AgentsPanel.tsx && git commit -m "feat(agent-mail): Email tab — wake list + send-to-anyone"`

---

### Task 13: Full verification, spec cleanup, PR

- [ ] **Step 1: backend full suite.** `cd apps/backend && uv run pytest tests/ -q` (fake AWS env per conftest; never SSO creds). All green.
- [ ] **Step 2: lint.** `cd apps/backend && uv run ruff check . && uv run ruff format --check .`; `cd apps/infra && pnpm exec tsc --noEmit`; `cd apps/frontend && pnpm exec tsc --noEmit && pnpm exec eslint src/components/control/panels`.
- [ ] **Step 3: docs.** Delete `docs/superpowers/specs/2026-08-21-agent-mail-inbox-design.md` and this plan (`git rm`) — the PR description is the durable record. Update CLAUDE.md only where it names deleted things: `agent_mail_relay.py`/`agent_mail_send.py` are not named in CLAUDE.md (verify with grep); the isol8 skill maintenance header was updated in Task 11.
- [ ] **Step 4: stray symlinks check.** `git ls-tree -r HEAD | awk '$1=="120000"{print $4}'` → empty.
- [ ] **Step 5: push + PR** (push requires the user's go — ask once, then): `git push -u origin feat/agent-mail-inbox` and `gh pr create --title "feat(agent-mail): inbox, allowlisted wake, one send door, SES tenants" --body-file <(…)` with: the policy table, the AWS pieces (config set, complaint → API destination, per-owner tenants + standard policy), deletions, and the dev verification checklist below. Then watch CI (`gh run view`, not `gh run watch`).
- [ ] **Step 6: dev verification after deploy (manual, in the PR description as a checklist):**
  1. Pick Friday in dev; Email tab → add `@github.com`? NO — add your own second address; Save → row minted; `isol8-mail.sh address` in the container prints it.
  2. Send a mail from an unlisted address → `aws dynamodb query --table-name isol8-dev-agent-mail-inbox --key-condition-expression 'address = :a'` shows it; Friday did not wake; `isol8-mail.sh inbox` shows it.
  3. Send from the listed address → Friday wakes; `send --reply <key>` threads back.
  4. Toggle "email anyone"; `send --to <you> --subject test` → arrives; `aws sesv2 get-tenant --tenant-name owner-<id>` exists with the standard policy (`aws sesv2 get-reputation-entity --reputation-entity-type RESOURCE --reputation-entity-reference <tenant-arn>`).
  5. Simulate a complaint with the SES mailbox simulator (`complaint@simulator.amazonses.com` as `--to`) → row flips `outbound_disabled=complaint`, owner gets the paused email, Email tab shows the banner, Re-enable clears it.
