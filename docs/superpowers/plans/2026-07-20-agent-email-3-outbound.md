# Provisioned Agent Email — Plan 3: Outbound

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An agent replies to an email by running a baked `isol8-mail` tool, which POSTs the reply-token + body to a backend relay; the relay decodes the token, verifies the calling container, and sends the reply via Resend from the owner's provisioned address, threaded into the original conversation. The agent holds no mail credentials; the relay can only ever send to the genuine original sender.

**Architecture:** `email_service.send_email` gains a `headers` param (for `In-Reply-To`/`References`). A new `agent_mail_reply` service decodes the reply-token, derives the from-address, builds threading headers + a `Re:` subject, and sends. A new authenticated relay router (`routers/agent_mail_relay.py`) authenticates the container by its `hook_token` (verified against the owner named in the decoded token), rate-limits per owner, and calls the service. A baked `isol8-mail.sh` script + one `SKILL.md` section give the agent the reply action — mirroring the proven `paperclip-api.sh` pattern.

**Tech Stack:** Python 3 / FastAPI (uv, pytest, respx/httpx), Resend SDK (`headers` supported), bash (`curl` + `jq`), the extended OpenClaw image.

**Design spec:** `docs/superpowers/specs/2026-07-19-provisioned-agent-email-design.md` §6.6.

## Global Constraints

- **Zero mail credentials in the container.** The container calls our relay with its existing `OPENCLAW_HOOK_TOKEN`; the relay is the only thing that touches Resend. Never bake a Resend key into the image.
- **Owner comes from the token, not the caller.** The container does not know its `owner_id` (no env var). The relay decodes `owner_id` from the reply-token, then verifies the presented `hook_token` equals `containers[owner_id]["hook_token"]` (timing-safe). A container can only reply for its own owner.
- **Reply only to the genuine sender.** The relay sends to `ctx["reply_to"]` from the decoded token — which the inbound leg set to the *parsed, allowlisted* sender. The agent cannot redirect a reply to an arbitrary address; it only supplies the token (opaque) + body.
- **Inert unless configured.** `send_email` already no-ops unless `RESEND_API_KEY` is a real `re_…` key. The relay additionally requires `AGENT_EMAIL_DOMAIN` set (so it only operates when the feature is live).
- **Threading:** set `In-Reply-To` and `References` to the token's `in_reply_to` (the original Message-ID); subject = `Re: {subject}` (do not double-prefix an existing `Re:`).
- **Rate limit** per owner on the relay, copying the in-process sliding-window idiom from `routers/hooks_proxy.py:47-73` (`_check_rate_limit`). Loop protection beyond this relies on the inbound `is_auto_submitted` guard (an owner's autoresponder is dropped on the way in); a per-thread reply counter is a documented v1 non-goal.
- Container→backend env facts (confirmed): `OPENCLAW_HOOK_TOKEN` (the credential) and `ISOL8_API_URL` (base, e.g. `https://api-dev.isol8.co`; routes hang off `/api/v1/...`) are injected; there is NO owner_id env var.
- Backend tests FOREGROUND, scoped. Full CI scope is `pytest tests/` (not `tests/unit`).
- Pre-existing unrelated failure to ignore: `tests/unit/routers/teams/test_agents.py::test_create_agent_returns_upstream_status_on_paperclip_error`.
- Commit after each task.

---

### Task 1: `send_email` supports custom headers

**Files:**
- Modify: `apps/backend/core/services/email_service.py` (add `headers` param + pass to `params`)
- Test: `apps/backend/tests/unit/services/test_email_service.py`

**Interfaces:**
- Produces: `send_email(..., headers: dict[str, str] | None = None, ...)` — when provided, sets `params["headers"] = headers` on the Resend send call. Existing behavior otherwise unchanged.

- [ ] **Step 1: Write the failing test.** Read the existing `test_email_service.py` for its mocking style (it patches `resend.Emails.send` and asserts the params). Add a test: calling `send_email(..., headers={"In-Reply-To": "<m@x>", "References": "<m@x>"})` results in the mocked `resend.Emails.send` being called with a params dict whose `headers` equals that dict. Add a second test that when `headers` is omitted, no `headers` key is present in params (no regression).

Run: `cd apps/backend && uv run pytest tests/unit/services/test_email_service.py -v --no-cov -p no:cacheprovider` (expect FAIL — `send_email` has no `headers` param yet / params lacks it).

- [ ] **Step 2: Implement.** Add `headers: dict[str, str] | None = None` to the `send_email` signature (keyword-only, in the existing `*` block). After the existing optional-param assignments (`text`, `reply_to`, `tags`), add:

```python
    if headers is not None:
        params["headers"] = headers
```

- [ ] **Step 3: Run — expect PASS.**

- [ ] **Step 4: Commit.**

```bash
git add apps/backend/core/services/email_service.py apps/backend/tests/unit/services/test_email_service.py
git commit -m "feat(agent-email): send_email supports custom headers (In-Reply-To/References)"
```

---

### Task 2: `agent_mail_reply` service — derive from-address, thread, send

**Files:**
- Create: `apps/backend/core/services/agent_mail_reply.py`
- Test: `apps/backend/tests/unit/services/test_agent_mail_reply.py`

**Interfaces:**
- Consumes: `agent_email_repo.query_by_owner`, `email_service.send_email`, `settings.AGENT_EMAIL_DOMAIN`.
- Produces:
  - `reply_subject(original: str) -> str` — `"Re: {original}"`, but returns `original` unchanged if it already starts with `re:` (case-insensitive, after strip).
  - `async resolve_from_address(owner_id: str, agent_id: str) -> str | None` — `query_by_owner(owner_id)`, return the `address` of the row with matching `agent_id` and `status == "active"`; None if no match.
  - `async send_reply(*, ctx: dict, body_text: str, body_html: str | None = None) -> str` — returns `"sent"`, `"no_address"`, `"disabled"`, or `"error"`. Never raises. `ctx` is a decoded `ReplyContext` (`owner_id, agent_id, reply_to, in_reply_to, subject`).

- [ ] **Step 1: Write the failing test.** Create `apps/backend/tests/unit/services/test_agent_mail_reply.py` covering:
  - `reply_subject("hello")` == `"Re: hello"`; `reply_subject("Re: hello")` == `"Re: hello"`; `reply_subject("re: hi")` == `"re: hi"` (no double prefix).
  - `resolve_from_address`: monkeypatch `agent_mail_reply.agent_email_repo.query_by_owner` to return `[{"address":"acme@d","agent_id":"a1","status":"active"}, {"address":"old@d","agent_id":"a1","status":"alias"}]`; assert `await resolve_from_address("o","a1") == "acme@d"` (picks active, not alias). With no matching agent_id → None.
  - `send_reply` happy path: monkeypatch `settings.AGENT_EMAIL_DOMAIN="agents.isol8.co"`, `resolve_from_address`→`"acme@agents.isol8.co"` (or the repo seam), and `email_service.send_email` to an AsyncMock returning `"email_id"`; call with `ctx={"owner_id":"o","agent_id":"a1","reply_to":"alice@x.com","in_reply_to":"<m@x>","subject":"Need help"}`; assert result `"sent"` and that `send_email` was called with `to="alice@x.com"`, `from_addr="acme@agents.isol8.co"`, `subject="Re: Need help"`, and `headers={"In-Reply-To":"<m@x>","References":"<m@x>"}`.
  - `send_reply` no address (resolve returns None) → `"no_address"`, `send_email` NOT called.
  - `send_reply` disabled (`AGENT_EMAIL_DOMAIN=""`) → `"disabled"`, `send_email` NOT called.
  - `send_reply` never raises: `send_email` raises → `"error"`.

Run: `cd apps/backend && uv run pytest tests/unit/services/test_agent_mail_reply.py -v --no-cov -p no:cacheprovider` (expect FAIL — ImportError).

- [ ] **Step 2: Implement `apps/backend/core/services/agent_mail_reply.py`.** `send_reply` first checks `settings.AGENT_EMAIL_DOMAIN` (empty → `"disabled"`), resolves the from-address (None → `"no_address"`), builds `headers = {"In-Reply-To": ctx["in_reply_to"], "References": ctx["in_reply_to"]}` (omit a header whose value is empty), builds a minimal HTML body from `body_html or body_text`, and calls `email_service.send_email(to=ctx["reply_to"], subject=reply_subject(ctx["subject"]), html=..., text=body_text, from_addr=from_addr, headers=headers, kind="agent_reply")`. Whole body wrapped so it never raises (`# noqa: BLE001` → `"error"`). Import the repo and email_service as modules so tests can monkeypatch their attributes.

- [ ] **Step 3: Run — expect PASS.**

- [ ] **Step 4: Commit.**

```bash
git add apps/backend/core/services/agent_mail_reply.py apps/backend/tests/unit/services/test_agent_mail_reply.py
git commit -m "feat(agent-email): reply-sending service (from-address, threading, Re: subject)"
```

---

### Task 3: the authenticated relay router

**Files:**
- Create: `apps/backend/routers/agent_mail_relay.py`
- Modify: `apps/backend/main.py` (import + mount)
- Test: `apps/backend/tests/unit/routers/test_agent_mail_relay.py`

**Interfaces:**
- Consumes: `agent_mail_token.decode_reply_token`, `container_repo.get_by_owner_id`, `agent_mail_reply.send_reply`, `settings`.
- Produces: `POST /api/v1/agent-mail/reply` — body `{reply_token: str, body_text: str, body_html?: str}`, header `Authorization: Bearer {hook_token}`. Mounted at prefix `/api/v1/agent-mail`.

**Auth + orchestration (mirror `routers/hooks_proxy.py` for the bearer + rate-limit idioms):**
1. Extract bearer token from `Authorization`; missing → 401.
2. Parse body; `ctx = decode_reply_token(reply_token)`; None → 400 `{"status":"bad_token"}` (an unforgeable token that won't decode is a bad request, not auth).
3. `container = await container_repo.get_by_owner_id(ctx["owner_id"])`; `expected = container.get("hook_token") if container else ""`; if not expected or not `hmac.compare_digest(presented, expected)` → 401 `{"status":"invalid_token"}`. (This is the load-bearing check: the caller must hold the hook_token of the owner named in the token.)
4. Rate-limit per `ctx["owner_id"]` (copy `hooks_proxy._check_rate_limit`); over → 429.
5. `result = await send_reply(ctx=ctx, body_text=body.body_text, body_html=body.body_html)`; return `{"status": result}` (200).
   Wrap steps 2-5 so an unexpected error returns 500 is acceptable here (this is an authenticated internal endpoint, not a webhook — a 5xx does not cause external retry storms), but prefer returning `{"status":"error"}` 200 for send failures (send_reply already maps those).

- [ ] **Step 1: Write the failing test.** Create `apps/backend/tests/unit/routers/test_agent_mail_relay.py` with `TestClient`. Monkeypatch `decode_reply_token`, `container_repo.get_by_owner_id`, and `send_reply` as attributes on the router module. Cover:
  - missing Authorization → 401.
  - bad token (`decode_reply_token`→None) → 400 `bad_token`, `send_reply` NOT called.
  - wrong hook_token (container row token != presented) → 401 `invalid_token`, `send_reply` NOT called.
  - no container row for owner → 401 `invalid_token`.
  - happy path (token decodes to owner "o"; container row hook_token matches presented) → 200 `{"status":"sent"}` and `send_reply` called once with the decoded ctx + body_text.
  - rate limit: exceed the limit → 429 (drive the loop N+1 times).

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_agent_mail_relay.py -v --no-cov -p no:cacheprovider` (expect FAIL).

- [ ] **Step 2: Implement `routers/agent_mail_relay.py`** per the orchestration; mount in `main.py`: `app.include_router(agent_mail_relay.router, prefix="/api/v1/agent-mail", tags=["agent-mail"])` + the import. Use a Pydantic body model (`reply_token`, `body_text`, `body_html: str | None = None`).

- [ ] **Step 3: Run — expect PASS.** Also `cd apps/backend && uv run python -c "import main"` (under the test env) to confirm the mount is clean.

- [ ] **Step 4: Commit.**

```bash
git add apps/backend/routers/agent_mail_relay.py apps/backend/main.py apps/backend/tests/unit/routers/test_agent_mail_relay.py
git commit -m "feat(agent-email): authenticated outbound reply relay"
```

---

### Task 4: the baked `isol8-mail` tool + skill section

**Files:**
- Create: `apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh` (executable)
- Modify: `apps/infra/openclaw/skills/isol8/SKILL.md` (new `## Replying by email` section + a MAINTENANCE-header anchor line)

**Reference:** `apps/infra/openclaw/skills/paperclip/scripts/paperclip-api.sh` — the proven pattern (reads a token from env, builds JSON with `jq -nc --arg`, `curl`s a backend with `Authorization: Bearer`, fails loudly on non-2xx). Mirror it.

**No Dockerfile change:** `COPY skills/isol8 /app/skills/isol8` already ships the new `scripts/` subdir, and the `skills/**` path filter in `build-openclaw-image.yml` already triggers a rebuild.

- [ ] **Step 1: Write `isol8-mail.sh`.** `#!/usr/bin/env bash`, `set -euo pipefail`. Contract:
  - Usage: `isol8-mail.sh <reply-token>`; the reply BODY is read from **stdin**.
  - Read `OPENCLAW_HOOK_TOKEN` and `ISOL8_API_URL` from env; if either is empty, print an error to stderr and exit non-zero.
  - Build the JSON body with `jq -nc --arg tok "$1" --arg body "$(cat)" '{reply_token:$tok, body_text:$body}'`.
  - `curl -sS -w '\n%{http_code}' -X POST "$ISOL8_API_URL/api/v1/agent-mail/reply" -H "Authorization: Bearer $OPENCLAW_HOOK_TOKEN" -H "Content-Type: application/json" -d "$json"`. Parse the trailing status code; on non-2xx print the response body to stderr and exit non-zero; on 2xx print a short success line.
  - `chmod +x` the file so `COPY` preserves the mode.

- [ ] **Step 2: Lint.** Run `shellcheck apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh` (install if needed: `brew install shellcheck`). Fix any warnings. Compare structure against `paperclip-api.sh` (same quoting discipline, same `jq -nc --arg` body building, same non-2xx handling).

- [ ] **Step 3: Add the SKILL.md section.** In `apps/infra/openclaw/skills/isol8/SKILL.md`, add a `## Replying by email` section stating: the agent has an email address (see `~/.openclaw/isol8-facts.json` → `email_address`); when it receives a message that begins with an email header block containing a `Reply-Token:`, it can reply to the sender by running `scripts/isol8-mail.sh <reply-token>` with the reply body on stdin (relative path — the agent resolves it against the skill dir). Note the reply goes only to the original sender and the agent must not paste the reply-token into visible text. Add a one-line anchor to the MAINTENANCE header comment (the `<!-- MAINTENANCE … -->` block) pointing at `routers/agent_mail_relay.py` + `core/services/agent_mail_reply.py` so the claim stays code-anchored.

- [ ] **Step 4: Commit.**

```bash
git add apps/infra/openclaw/skills/isol8/scripts/isol8-mail.sh apps/infra/openclaw/skills/isol8/SKILL.md
git commit -m "feat(agent-email): baked isol8-mail reply tool + skill section"
```

---

### Task 5: full verification

- [ ] **Step 1:** `cd apps/backend && uv run pytest tests/ -q --no-cov -p no:cacheprovider` — expect only the known pre-existing failure. (Full scope; backgrounds; read the summary line, don't trust a piped exit code.)
- [ ] **Step 2:** Confirm still inert: `grep -rn "AGENT_EMAIL_DOMAIN\|RESEND_API_KEY\|RESEND_WEBHOOK_SECRET" apps/infra/lib || echo "unset — inert"` (RESEND_API_KEY may legitimately be a CDK secret *name* reference — confirm no real value is hardcoded).
- [ ] **Step 3:** `git add -A && git commit -m "chore(agent-email): outbound lint fixups" || echo "nothing to commit"`.

---

## Done — What Ships

The outbound half, still inert. When the activation runbook sets the secrets, promotes the image (so `isol8-mail.sh` reaches containers), and reprovisions, an agent that received an email can reply: it runs `isol8-mail.sh <token>`, the relay verifies the container by its hook_token, and Resend sends the reply from the owner's address, threaded, to the genuine sender — with no mail credentials ever in the container.

**Next (Plan 4):** attachments (S3 store-and-link on the inbound side), plus the backfill script for existing owners and the activation runbook (verify Resend receiving domain, set secrets, promote image, reprovision, live-test).
