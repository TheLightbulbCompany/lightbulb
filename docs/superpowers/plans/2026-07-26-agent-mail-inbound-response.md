# Agent Mail Inbound Response Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every inbound the owner sends produce a response — never a silent drop.

**Architecture:** Two silent drops on the inbound path become answered states. An over-cap owner gets a server-templated auto-reply (no agent run, nothing charged). A sleeping container is woken and the message delivered, with a footer on the eventual reply telling a free-tier owner why it was slow.

**Tech Stack:** FastAPI, DynamoDB via repositories, AWS SES v2, pytest.

## Global Constraints

- `AGENT_EMAIL_DOMAIN` gates everything. Empty ⇒ every new path short-circuits inert.
- **Only `TRIAL_EXPIRED` may present payment as REQUIRED.** `DAILY_CAP` may *offer* upgrading, after the free path, never as the way out — waiting genuinely works.
- Unknown-recipient and unverified-sender **must stay silent**. Replying confirms the address exists and turns the endpoint into a reflector.
- Auto-replies are server-templated with **no agent run** — SES sending is not gated by the trial cap.
- Nothing on this path may raise: the ingest webhook must stay terminal.
- Reason constants are `trial_cap.TRIAL_EXPIRED` (`"trial_expired"`) and `trial_cap.DAILY_CAP` (`"trial_daily_cap"`). Never hardcode the strings.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. CI scope is `tests/`, not `tests/unit/`.
- Any new route or auth-dependent endpoint must be added to BOTH schemathesis exclusion surfaces if it reads DDB before auth completes — `tests/contract/test_api_contracts.py` and `tests/chaos/api/conftest.py`. (No new routes in this plan; noted because the last PR was caught by exactly this.)
- Commit messages end with:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

## PR boundary

**Tasks 1–2 are independently shippable** and stop the silent cap drop on their own. Land them first; tasks 3–6 (wake + footer) are coupled and can follow in a second PR.

---

### Task 1: The auto-reply service

**Files:**
- Create: `apps/backend/core/services/agent_mail_autoreply.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_autoreply.py`

**Interfaces:**
- Consumes: existing `ses_email.send_ses_reply`, `trial_cap.TRIAL_EXPIRED`, `trial_cap.DAILY_CAP`.
- Produces: `async def send_cap_autoreply(*, reason: str, from_addr: str, to: str, in_reply_to: str | None, subject: str) -> bool` — True when SES accepted. Never raises.

- [ ] **Step 1: Write the failing tests**

```python
import pytest

from core.services import agent_mail_autoreply as mod
from core.services.trial_cap import DAILY_CAP, TRIAL_EXPIRED


@pytest.fixture
def sent(monkeypatch):
    box = []

    async def _send(*, from_addr, to, subject, body_text, body_html, headers):
        box.append({"from": from_addr, "to": to, "subject": subject, "body": body_text, "headers": headers})
        return "msg-1"

    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _send)
    return box


async def _call(reason):
    return await mod.send_cap_autoreply(
        reason=reason,
        from_addr="friday@acme.isol8.co",
        to="owner@example.com",
        in_reply_to="<m1@mail>",
        subject="Re: hello",
    )


@pytest.mark.asyncio
async def test_daily_cap_says_it_resets_and_does_NOT_demand_payment(sent):
    assert await _call(DAILY_CAP) is True
    body = sent[0]["body"].lower()
    assert "resets" in body
    # Waiting must be offered FIRST and upgrading must not be the only way out.
    assert "send this again" in body
    assert "must upgrade" not in body
    assert "your free plan has ended" not in body


@pytest.mark.asyncio
async def test_trial_expired_does_ask_for_payment(sent):
    assert await _call(TRIAL_EXPIRED) is True
    body = sent[0]["body"].lower()
    assert "ended" in body
    assert "upgrade" in body
    assert "resets" not in body


@pytest.mark.asyncio
async def test_threads_with_the_conversation(sent):
    await _call(DAILY_CAP)
    assert sent[0]["from"] == "friday@acme.isol8.co"
    assert sent[0]["to"] == "owner@example.com"
    assert sent[0]["headers"]["In-Reply-To"] == "<m1@mail>"


@pytest.mark.asyncio
async def test_unknown_reason_sends_nothing(sent):
    assert await _call("something_else") is False
    assert sent == []


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(monkeypatch, sent):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _call(DAILY_CAP) is False
    assert sent == []


@pytest.mark.asyncio
async def test_ses_failure_returns_false_and_does_not_raise(monkeypatch, sent):
    async def _boom(**kwargs):
        raise RuntimeError("ses down")

    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _boom)
    assert await _call(DAILY_CAP) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_autoreply.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.agent_mail_autoreply'`

- [ ] **Step 3: Implement**

```python
"""Server-templated replies for inbound the agent cannot answer.

Sent when a free-tier owner emails their agent but no run is possible. There
is NO agent run here: SES sending is not gated by the trial cap (the cap
fences Bedrock inference), so we can always answer even when we cannot think.
Nothing is reserved and nothing is spent.

Only ever sent to the OWNER. `routers/ses_inbound.py` enforces a sender
allowlist before this point, so an inbound that reaches the cap check is
authenticated. Unknown-recipient and unverified-sender inbound must stay
silent — replying there would confirm the address exists and turn the
endpoint into a reflector.
"""

import logging

from core.config import settings
from core.observability.metrics import put_metric
from core.services import ses_email
from core.services.trial_cap import DAILY_CAP, TRIAL_EXPIRED

logger = logging.getLogger(__name__)

SETTINGS_URL = "https://isol8.co/settings"

# DAILY_CAP resets at 00:00 UTC, so waiting is a real answer and is offered
# FIRST. Upgrading appears only as an alternative. Telling this owner they
# must pay would simply be false.
_DAILY_CAP_BODY = (
    "I've used today's free allowance, so I can't pick this up right now.\n\n"
    "It resets at 00:00 UTC — send this again after that, or upgrade to remove "
    f"the limit: {SETTINGS_URL}\n"
)

# TRIAL_EXPIRED has nothing to wait for. This is the only state that may
# present payment as required.
_TRIAL_EXPIRED_BODY = (
    "Your free plan has ended, so I can't run right now.\n\n"
    f"Upgrade and I'll pick this straight back up: {SETTINGS_URL}\n"
)

_BODIES = {DAILY_CAP: _DAILY_CAP_BODY, TRIAL_EXPIRED: _TRIAL_EXPIRED_BODY}


async def send_cap_autoreply(
    *, reason: str, from_addr: str, to: str, in_reply_to: str | None, subject: str
) -> bool:
    """Answer an inbound that cannot be run. True when SES accepted it.

    Never raises — the ingest webhook must stay terminal.
    """
    try:
        if not (settings.AGENT_EMAIL_DOMAIN or "").strip():
            return False

        body = _BODIES.get(reason)
        if body is None:
            # An unrecognised reason must not produce a generic upgrade nudge:
            # we would be guessing whether the owner has to pay.
            logger.warning("agent_mail_autoreply: no template for reason %r", reason)
            return False

        headers = {"In-Reply-To": in_reply_to, "References": in_reply_to} if in_reply_to else None

        message_id = await ses_email.send_ses_reply(
            from_addr=from_addr,
            to=to,
            subject=subject,
            body_text=body,
            body_html=None,
            headers=headers,
        )
        ok = message_id is not None
        put_metric(
            "agent_email.autoreply",
            dimensions={"reason": reason, "result": "sent" if ok else "failed"},
        )
        return ok
    except Exception:
        logger.exception("agent_mail_autoreply failed for reason=%s", reason)
        put_metric("agent_email.autoreply", dimensions={"reason": reason, "result": "error"})
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_autoreply.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
cd apps/backend && uv run ruff format core/services/agent_mail_autoreply.py tests/unit/services/test_agent_mail_autoreply.py
git add apps/backend/core/services/agent_mail_autoreply.py apps/backend/tests/unit/services/test_agent_mail_autoreply.py
git commit -m "feat(agent-mail): server-templated auto-reply for un-runnable inbound

No agent run: SES sending is not gated by the trial cap, so we can always
answer even when we cannot think. Nothing is reserved or spent.

DAILY_CAP and TRIAL_EXPIRED get DIFFERENT copy on purpose. DAILY_CAP resets at
00:00 UTC, so waiting is a real answer and is offered first; upgrading appears
only as an alternative. Telling that owner they must pay would be false, and
false urgency is trivially caught. TRIAL_EXPIRED has nothing to wait for and is
the only state that may present payment as required.

An unrecognised reason sends NOTHING rather than a generic upgrade nudge --
that would be guessing whether the owner has to pay.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire it into the over-cap branch

**Files:**
- Modify: `apps/backend/routers/ses_inbound.py` (the `dropped_over_cap` branch, ~line 150)
- Test: `apps/backend/tests/unit/routers/test_ses_inbound.py`

**Interfaces:**
- Consumes: `agent_mail_autoreply.send_cap_autoreply` (Task 1).
- Produces: the over-cap branch returns `{"status": "capped_notified"}` when the auto-reply was sent, `{"status": "dropped_over_cap"}` when it was not.

**Before writing this**, read the branch and the surrounding names:

```bash
cd apps/backend && sed -n '140,155p' routers/ses_inbound.py
```

`row` carries `address` and `owner_id`; `body.from_` is the sender; `body.subject` and `body.message_id` are available for threading.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_over_cap_sends_an_autoreply_and_reports_capped_notified(client, monkeypatch, capped_owner):
    calls = []

    async def _autoreply(*, reason, from_addr, to, in_reply_to, subject):
        calls.append({"reason": reason, "from": from_addr, "to": to})
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)
    r = await post_inbound(client)
    assert r.json()["status"] == "capped_notified"
    assert len(calls) == 1
    # Sent FROM the agent's own address, TO the owner who wrote in.
    assert calls[0]["from"] == "friday@acme.isol8.co"
    assert calls[0]["to"] == "owner@example.com"


@pytest.mark.asyncio
async def test_over_cap_still_reports_dropped_when_the_autoreply_fails(client, monkeypatch, capped_owner):
    async def _autoreply(**kwargs):
        return False

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)
    r = await post_inbound(client)
    assert r.json()["status"] == "dropped_over_cap"


@pytest.mark.asyncio
async def test_over_cap_never_delivers_to_the_agent(client, monkeypatch, capped_owner):
    delivered = []

    async def _deliver(**kwargs):
        delivered.append(kwargs)
        return "delivered"

    async def _autoreply(**kwargs):
        return True

    monkeypatch.setattr(ses_inbound, "deliver_to_agent", _deliver, raising=False)
    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)
    await post_inbound(client)
    assert delivered == []


@pytest.mark.asyncio
async def test_unverified_sender_gets_NO_autoreply(client, monkeypatch):
    """Replying would confirm the address exists and make this a reflector."""
    calls = []

    async def _autoreply(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)
    r = await post_inbound(client, from_="stranger@example.com")
    assert r.json()["status"] == "dropped_unverified_sender"
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_recipient_gets_NO_autoreply(client, monkeypatch):
    calls = []

    async def _autoreply(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(ses_inbound, "send_cap_autoreply", _autoreply)
    r = await post_inbound(client, to=["nobody@nowhere.isol8.co"])
    assert r.json()["status"] == "dropped_unknown_recipient"
    assert calls == []
```

Reuse this file's existing fixtures for `client` and the signed-request helper; add a `capped_owner` fixture that patches `ses_inbound.trial_block_reason` to return `trial_cap.DAILY_CAP` and `get_by_address` to return a row with `address="friday@acme.isol8.co"`, `owner_id="user_a"`, `verified_senders=["owner@example.com"]`, `public=False`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_ses_inbound.py -k "capped or autoreply or NO_autoreply" -v`
Expected: FAIL — the branch still returns `dropped_over_cap` unconditionally

- [ ] **Step 3: Implement**

Replace the over-cap branch:

```python
        # Free-tier cost cap — same predicate the chat/container-start
        # gates use, so an over-cap owner can't route around it via email.
        #
        # ANSWERED, not silently dropped. The sender allowlist above already
        # proved this is the OWNER, so replying is safe here in a way it is
        # NOT for the two drops above (replying to those would confirm the
        # address exists and turn this into a reflector).
        #
        # The reply is server-templated with no agent run — that is the whole
        # reason we can answer at all when the cap says we cannot think.
        cap_reason = await trial_block_reason(row["owner_id"])
        if cap_reason is not None:
            notified = await send_cap_autoreply(
                reason=cap_reason,
                from_addr=row["address"],
                to=sender,
                in_reply_to=body.message_id,
                subject=reply_subject(body.subject or ""),
            )
            return {"status": "capped_notified" if notified else "dropped_over_cap"}
```

Add to the imports at the top of the file:

```python
from core.services.agent_mail_autoreply import send_cap_autoreply
```

`reply_subject` is already imported by this module's neighbours; if it is not
imported here, add `from core.services.agent_mail_reply import reply_subject`.

- [ ] **Step 4: Run the whole inbound suite**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_ses_inbound.py -v`
Expected: PASS — including the pre-existing tests, one of which asserts the old
`dropped_over_cap` string. Update that test to expect `capped_notified` when an
auto-reply succeeds; do **not** xfail it.

- [ ] **Step 5: Commit**

```bash
cd apps/backend && uv run ruff format routers/ses_inbound.py tests/unit/routers/test_ses_inbound.py
git add apps/backend/routers/ses_inbound.py apps/backend/tests/unit/routers/test_ses_inbound.py
git commit -m "feat(agent-mail): answer an over-cap inbound instead of dropping it silently

The owner emailed their own agent and got nothing back, forever, with no
bounce -- SES had already accepted the message. Now they get a reply that says
which limit they hit and what actually clears it.

Safe here specifically because the sender allowlist above already proved this
is the OWNER. The unknown-recipient and unverified-sender drops stay SILENT and
have tests asserting no auto-reply: replying to those would confirm the address
exists and turn this endpoint into a reflector.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Second PR: wake + footer (Tasks 3–6)

### How delivery completes after a wake — RESOLVED, build nothing

The spec asked for a persisted pending-delivery marker flushed "when the
gateway connection comes up". **Do not build that.** The retry it describes
already exists, assembled from two things already in the codebase:

1. **SES invokes the ingest Lambda asynchronously.** `ses-inbound-stack.ts`
   passes no `invocationType`, and CDK's default is `Event` (see
   `aws-ses-actions/lib/lambda.d.ts`: `@default Event`). Async invocation means
   **Lambda itself retries on function error** — 2 retries by default, roughly
   1 and 2 minutes later.
2. **The ingest Lambda already re-raises on 5xx**, by design — see
   `_post_to_backend`'s docstring: *"A 5xx response means the backend is
   signalling a transient failure, so it is RE-RAISED … so SES retries."*

So the whole mechanism is:

```
asleep + under cap  ->  start service, stamp cold-wake, return 503
                    ->  Lambda re-raises -> async invocation fails
                    ->  Lambda retries ~1 min later
                    ->  Fargate cold start (~40s) done -> delivered
```

No pending store, no gateway hook, no background task, and **nothing held in
memory** — a backend restart loses nothing, because the retry re-reads the same
S3 object. Double-delivery is already safe: `idempotency_key` is the SES
`messageId`, which OpenClaw dedupes on.

**Known limit:** async retries default to 2, so ~3 attempts over ~3 minutes. A
container slower than that loses the message. Task 4 Step 6 checks whether the
ingest Lambda has a DLQ; if not, that is the follow-up — a smaller and more
honest piece of work than inventing a delivery queue.

### Task 3: `email_cold_wake_at` stamp on the containers row

**Files:**
- Modify: `apps/backend/core/repositories/container_repo.py`
- Test: `apps/backend/tests/unit/repositories/test_container_repo.py`

**Interfaces:**
- Produces:
  - `async def stamp_email_cold_wake(owner_id: str) -> None`
  - `async def consume_email_cold_wake(owner_id: str, *, max_age_seconds: int = 3600) -> bool` — True when a stamp existed and was recent; clears it either way.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_cold_wake_stamp_is_consumed_once(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    assert await container_repo.consume_email_cold_wake("user_a") is True
    # Second read must be False -- the footer appears exactly once.
    assert await container_repo.consume_email_cold_wake("user_a") is False


@pytest.mark.asyncio
async def test_cold_wake_stamp_expires(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    await container_repo.stamp_email_cold_wake("user_a")

    # A stale stamp must not attach a footer to an unrelated reply days later.
    assert await container_repo.consume_email_cold_wake("user_a", max_age_seconds=0) is False


@pytest.mark.asyncio
async def test_no_stamp_is_false_not_an_error(dynamodb_table):
    from core.repositories import container_repo

    await container_repo.upsert("user_a", {"hook_token": "t"})
    assert await container_repo.consume_email_cold_wake("user_a") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo.py -k cold_wake -v`
Expected: FAIL with `AttributeError: ... has no attribute 'stamp_email_cold_wake'`

- [ ] **Step 3: Implement**

```python
async def stamp_email_cold_wake(owner_id: str) -> None:
    """Record that an inbound email cold-woke this container.

    Read once by ``agent_mail_reply.send_reply`` to decide whether the agent's
    outgoing reply should carry the free-tier "I was asleep" footer. The wake
    and the reply happen in DIFFERENT requests, which is why this has to be
    persisted rather than passed. Never raises.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET email_cold_wake_at = :t",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":t": int(time.time())},
        )
    except ClientError as e:
        logger.warning("stamp_email_cold_wake failed for %s: %s", owner_id, e)


async def consume_email_cold_wake(owner_id: str, *, max_age_seconds: int = 3600) -> bool:
    """True when a RECENT cold-wake stamp existed. Clears it either way.

    Clearing unconditionally is deliberate: the footer must appear exactly
    once, and a stale stamp must not attach to an unrelated reply days later.
    Never raises.
    """
    try:
        row = await get_by_owner_id(owner_id)
        stamped = (row or {}).get("email_cold_wake_at")
        if stamped is None:
            return False
        fresh = (int(time.time()) - int(stamped)) <= max_age_seconds
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE email_cold_wake_at",
            ConditionExpression="attribute_exists(owner_id)",
        )
        return fresh
    except ClientError as e:
        logger.warning("consume_email_cold_wake failed for %s: %s", owner_id, e)
        return False
```

`time` and `ClientError` are already imported by this module.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/repositories/test_container_repo.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd apps/backend && uv run ruff format core/repositories/container_repo.py tests/unit/repositories/test_container_repo.py
git add apps/backend/core/repositories/container_repo.py apps/backend/tests/unit/repositories/test_container_repo.py
git commit -m "feat(agent-mail): cold-wake stamp so the reply can explain the delay

The wake and the agent's reply happen in DIFFERENT requests, so the fact that
this inbound needed a cold start has to be persisted rather than passed.

consume_ clears the stamp whether or not it was fresh: the footer must appear
exactly once, and a stale stamp must never attach to an unrelated reply days
later.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wake on inbound

**Files:**
- Modify: `apps/backend/core/services/agent_mail_delivery.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_delivery.py`

**Interfaces:**
- Consumes: `container_repo.stamp_email_cold_wake` (Task 3); existing `get_ecs_manager().start_user_service`, `trial_cap.trial_block_reason`.
- Produces: `deliver_to_agent` gains return value `"woken"` alongside the existing `"delivered"`, `"container_not_running"`, `"error"`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.asyncio
async def test_running_container_delivers_without_waking(delivery_harness):
    delivery_harness.ip = "10.0.0.1"
    assert await call_deliver() == "delivered"
    assert delivery_harness.started is False
    assert delivery_harness.stamped is False


@pytest.mark.asyncio
async def test_asleep_and_under_cap_starts_the_service_and_stamps(delivery_harness):
    delivery_harness.ip = None
    delivery_harness.cap_reason = None
    assert await call_deliver() == "woken"
    assert delivery_harness.started is True
    assert delivery_harness.stamped is True


@pytest.mark.asyncio
async def test_asleep_and_over_cap_does_NOT_wake(delivery_harness):
    """Cost stays fenced exactly as chat is."""
    delivery_harness.ip = None
    delivery_harness.cap_reason = "trial_daily_cap"
    assert await call_deliver() == "container_not_running"
    assert delivery_harness.started is False
    assert delivery_harness.stamped is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_delivery.py -v`
Expected: FAIL — `deliver_to_agent` returns `container_not_running` and never starts anything

- [ ] **Step 3: Implement**

In `deliver_to_agent`, replace the early return:

```python
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not ip:
            # Asleep. Waking is cost, so it goes through the SAME cap
            # predicate chat and container-start use -- an over-cap owner is
            # never woken by mail.
            if await trial_block_reason(owner_id) is not None:
                return "container_not_running"
            await get_ecs_manager().start_user_service(owner_id)
            await container_repo.stamp_email_cold_wake(owner_id)
            # Delivery is completed by SES's OWN retry, not by anything here.
            # The caller maps "woken" to a 503, the ingest Lambda re-raises on
            # 5xx, and its async invocation is retried ~1 min later -- by which
            # time the Fargate cold start (~40s) has finished. Nothing is
            # queued or held in memory, so a backend restart loses nothing.
            return "woken"
```

The caller in `routers/ses_inbound.py` must map `"woken"` to **HTTP 503**, or
the retry never happens and the wake is pointless:

```python
        result = await deliver_to_agent(...)
        if result == "woken":
            # Signal transient to the ingest Lambda so it re-raises and its
            # async invocation is retried once the container is up.
            raise HTTPException(status_code=503, detail={"status": "waking"})
```

Update the module docstring: the "Never wakes a scaled-to-zero container"
paragraph is now wrong and must state what replaced it, including that
delivery completes via SES retry rather than in-process.

- [ ] **Step 6: Check the DLQ**

```bash
aws lambda get-function-configuration \
  --function-name isol8-prod-ses-inbound-SesIngestFunctionE2A868B7-ct40A0yBsZWn \
  --query '{DLQ:DeadLetterConfig,Retries:null}' --output json
aws lambda get-function-event-invoke-config \
  --function-name isol8-prod-ses-inbound-SesIngestFunctionE2A868B7-ct40A0yBsZWn 2>&1 | head -5
```

Async retries default to 2, so a container slower than ~3 minutes drops the
message. If there is no DLQ, record it as a follow-up — do NOT expand this task
into building one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_delivery.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd apps/backend && uv run ruff format core/services/agent_mail_delivery.py tests/unit/services/test_agent_mail_delivery.py
git add apps/backend/core/services/agent_mail_delivery.py apps/backend/tests/unit/services/test_agent_mail_delivery.py
git commit -m "feat(agent-mail): wake a sleeping container for inbound instead of dropping it

The docstring called the drop intentional. It is defensible for a general hook
and wrong for email: the agent's own message invites a reply, and on the free
tier that reply vanished with no bounce, because SES had already accepted it.

Waking is cost, so it runs through the SAME cap predicate chat and
container-start use -- an over-cap owner is never woken by mail, and gets the
auto-reply instead.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: The free-tier footer

**Files:**
- Modify: `apps/backend/core/services/agent_mail_reply.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_reply.py`

**Interfaces:**
- Consumes: `container_repo.consume_email_cold_wake` (Task 3); `billing_repo.get_by_owner_id`.
- Produces: no new public symbols; `send_reply`'s body gains the footer conditionally.

- [ ] **Step 1: Write the failing tests**

These use a `reply_harness` fixture and a `send()` helper you must write, in
the style this test file already uses for `send_reply`: the harness
monkeypatches `container_repo.consume_email_cold_wake` (returns
`harness.cold_wake`), `billing_repo.get_by_owner_id` (returns
`{"subscription_status": harness.subscription_status}`), and
`ses_email.send_ses_reply` (records the body as `harness.sent_body`).
`send()` calls `send_reply` with a ctx whose `owner_id` is `"user_a"` and a
`body_text` of `"agent words"`.

```python
FOOTER_MARK = "I was asleep when this arrived"


@pytest.mark.asyncio
async def test_footer_when_free_tier_and_recent_cold_wake(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = True
    await send()
    assert FOOTER_MARK in reply_harness.sent_body
    # The agent's own words must be untouched, above the separator.
    assert reply_harness.sent_body.index("agent words") < reply_harness.sent_body.index(FOOTER_MARK)


@pytest.mark.asyncio
async def test_no_footer_without_a_cold_wake(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = False
    await send()
    assert FOOTER_MARK not in reply_harness.sent_body


@pytest.mark.asyncio
async def test_no_footer_for_a_paid_owner(reply_harness):
    """A paid container never sleeps, so the note would be a lie."""
    reply_harness.subscription_status = "active"
    reply_harness.cold_wake = True
    await send()
    assert FOOTER_MARK not in reply_harness.sent_body


@pytest.mark.asyncio
async def test_footer_appears_exactly_once(reply_harness):
    reply_harness.subscription_status = "trial_local"
    reply_harness.cold_wake = True
    await send()
    assert reply_harness.sent_body.count(FOOTER_MARK) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_reply.py -k footer -v`
Expected: FAIL — no footer is ever appended

- [ ] **Step 3: Implement**

```python
_COLD_WAKE_FOOTER = (
    "\n\n———\n"
    "I was asleep when this arrived — on the free plan I sleep when idle, so "
    "replies wait for me to wake. https://isol8.co/settings\n"
)


async def _maybe_cold_wake_footer(owner_id: str) -> str:
    """The free-tier "I was asleep" note, or "".

    Consumed (and cleared) here so it appears exactly once. Free tier only: a
    paid container never sleeps, so the note would be a lie. Never raises — a
    footer must not cost a reply.
    """
    try:
        if not await container_repo.consume_email_cold_wake(owner_id):
            return ""
        account = await billing_repo.get_by_owner_id(owner_id) or {}
        if account.get("subscription_status") != "trial_local":
            return ""
        return _COLD_WAKE_FOOTER
    except Exception:  # noqa: BLE001
        logger.warning("cold-wake footer lookup failed for %s", owner_id)
        return ""
```

Then in `send_reply`, after `body_text` is known and before the SES call:

```python
        body_text = body_text + await _maybe_cold_wake_footer(ctx["owner_id"])
```

- [ ] **Step 4: Run the whole reply suite**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_reply.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd apps/backend && uv run ruff format core/services/agent_mail_reply.py tests/unit/services/test_agent_mail_reply.py
git add apps/backend/core/services/agent_mail_reply.py apps/backend/tests/unit/services/test_agent_mail_reply.py
git commit -m "feat(agent-mail): tell a free-tier owner their agent had been asleep

Surfaced on the reply that actually arrives, so the promise the agent's email
makes is kept AND the limitation is felt at the exact moment it bit. An LLM
might mention being asleep; a footer always does, and is testable.

Free tier only -- a paid container never sleeps, so the note would be a lie.
The stamp is consumed here, so it appears exactly once.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1: Run the non-chaos backend suite**

Run: `cd apps/backend && uv run pytest tests/ --ignore=tests/chaos -q --no-cov`
Expected: PASS. This takes ~10 minutes.

- [ ] **Step 2: Run the chaos negative pass**

Run: `cd apps/backend && uv run pytest tests/chaos/api/test_contract_negative.py -q --no-cov`
Expected: PASS. No new routes were added, so no new exclusions should be needed
— if one IS needed, add it to BOTH `tests/contract/test_api_contracts.py` and
`tests/chaos/api/conftest.py`, with a written reason.

- [ ] **Step 3: Commit any fixes and open the PR**

## Out of scope

- Queueing a capped message until the cap resets (spec decision 4 — the owner is told to resend).
- Making addresses public.
- Any in-app upgrade prompt; this is email only.
