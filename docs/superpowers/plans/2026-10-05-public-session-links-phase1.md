# Public Session Links, Phase 1 (Continue) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each session has one share link with an audience (Workspace only / Anyone). The link opens a page for live and ended sessions. A signed-in receiver can take a redacted copy and resume it with `lightbulb continue '<link>'` in their own Claude Code. The copy's code follows the public/private/none rules from the sharer's Mac, and the receiver's terminal shows a "Continued from" band.

**Architecture:** The link lives on the session's META row (`shareLink`) with a `SHARELINK#<hash>` lookup row. On a Claude session the link also turns pick-up on, so it reuses the pick-up snapshot, manifest, presigned URLs and sweeper. A new backend module, `session_links.py`, covers the link holder's side: the public page model, events, copies and redeem. A new `session_redact.py` redacts everything an outsider receives. On the desktop, `snapshot.rs` reports `repoInfo` on every pick-up commit. A new `session_continue.rs` reuses `session_pickup.rs` to download, verify, get the code and run `claude --resume … --fork-session`. `cli.rs` routes `continue` to that module. The mod reads a marker file and draws the band. The web gets a Share control in the session's Access tab and a public `/s` page that also serves as the CLI confirm page.

**Tech Stack:** FastAPI + DynamoDB (moto in tests) + S3, pytest; Rust (Tauri crate `lightbulb_desktop`, tokio, reqwest), `cargo test --lib`; Claude Code mod (TSX), `claude plugin test`; Next.js 16 + vitest; CDK (api-stack).

**Spec:** `docs/superpowers/specs/2026-10-05-public-session-links.md` (§3 is Phase 1; §4 and §5 are out of scope).

## Global Constraints

- Phase 1 only. No anonymous live watching (no AppSync API key, no `pub` namespace, no `LINKVIEWER#`/`PUBWATCH#`), no guest steering, no `guests`, no join-request changes.
- One link per session. Audience is exactly `"workspace" | "anyone"`. Default `"workspace"`.
- Link token: `secrets.token_urlsafe(32)`. It goes only in the URL fragment and in POST bodies. It is never logged. Every failure to open reads the same: `"This link was turned off or is not for you."`
- `META.shareLink.tokenHash` is the truth. An open whose hash does not match it is refused, whatever lookup row was found.
- An outsider never receives an owner id, user id, workspace id, Lightbulb session id, cwd or absolute path. A member's page model may carry `sessionId` (their in-app paths need it).
- Private repos stay private. An outsider never receives the remote, repo name or branch of a repo whose `repoInfo.public` is false.

**Controller rulings (2026-10-05), binding:**

- **Domain.** Links and copy use `settings.FRONTEND_URL` (`https://thelightbulb.company`, dev `https://dev.thelightbulb.company`), never `flickmeon.ai`. The link URL is `settings.FRONTEND_URL.rstrip('/') + "/s#" + token`; the mod's band says `thelightbulb.company`. No literal domain in backend code.
- **Private repo, "Anyone".** For an outsider, a session whose `repoInfo.public` is false has its working-folder name (`repoInfo.folder`, the repo's top-level folder) replaced by `<project>` wherever it is a path component: each transcript line's `cwd`, paths in events, the title. This applies to the copy and to the page. Code that Claude read inside the transcript is shared by the owner's choice; only metadata (remote, name, branch, folder) is withheld. When the sharer picks "Anyone" for such a session, the warning line reads: "This conversation includes code Claude read from your private repository."
- **Deploy order.** The backend that accepts `CommitRequest.repo` ships to **prod** before a desktop build that sends `repo`. `ContractModel` is `extra="forbid"` (`messaging.py:205`), so an older backend answers 400 to the new field and the Mac's snapshot fails.
- **`cli.rs`.** No open PR touches it (checked 2026-10-05). Task 7 edits it directly.
- **Probe.** The public probe runs with `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1` (Task 5), so a global `url.<ssh>.insteadOf` cannot turn it into an authenticated call.

**Other constraints:**

- Pick up and member copies are not redacted. Outsider copies and outsider event pages are redacted.
- Copies: row TTL 1 h (`COPY_TTL_S = 3600`). A code can be redeemed for 10 min after it was created or became ready, whichever is later (`CODE_WINDOW_S = 600`). A `preparing` copy older than 5 min is restarted (`RESTART_AFTER_S = 300`). At most one redaction runs per backend task (`asyncio.Semaphore(1)`).
- Copy objects go under `session-pickup/<owner>/<sid>/<epoch>/copies/<copyId>/`, never under `<owner>/`.
- Codex sessions get Read only. Their link never calls `set_pickup`, and Continue refuses them.
- Any change under `apps/infra/lib/` requires `cd apps/infra && npx cdk synth`, and a changed `.github/workflows/deploy.yml` is committed in the same PR.
- Tests: CI is the gate. Each task runs only its own targeted tests. Do not run `sleep` or `tail` in bash.
- Commits end with the two attribution lines from the session's system reminder.

## Review Focus

1. **A user's global git `url.<base>.insteadOf` rewrites `https://` to SSH.** The "anonymous" probe would then use their keys, and a private repo would read as public. The probe must ignore global and system git config. This is pinned in Task 5 (`probe_command` test).
2. **The owner's display name falls back to the Clerk user id.** `human_display_name` returns `user_id` when Clerk has no name (`messaging.py:1449-1451`). The page and the copy ticket must say "Someone" rather than leak `user_…`. This is pinned in Task 3 (`test_owner_name_never_leaks_a_user_id`).
3. **An event near the 16 KiB envelope can grow when it is redacted.** For example, `://a:b@` becomes `://[redacted]@`. That can fail `LocalSessionEvent` validation and turn the whole page into a 500. Such an event falls back to one `[redacted]` line. This is pinned in Task 3.
4. **A transcript line can span two parts.** The Mac cuts at `CHUNK` when a single line is longer than a chunk (`snapshot.rs:306-309`). Per-part redaction must carry the partial line into the next part. This is pinned in Task 4.
5. **The owner can turn the link off after a continue code was issued but before the CLI redeems it.** Redeem must then refuse, because "no new copy can be made". This is pinned in Task 4.
6. **Before the Mac's first snapshot the server does not know whether the repo is private.** An outsider's event page for a Claude session therefore stays empty until the first commit (`pickupSnapshot.at`), or the private folder name would leak in that window. This is pinned in Task 3.

---

## File map

| File | Change |
|---|---|
| `apps/backend/core/services/session_redact.py` | Create: `redact`, `redact_value` (both take an optional private `project` folder) |
| `apps/backend/core/services/local_sessions.py` | Replace the share-link section (`:2227-2440`) with `put_link`/`get_link`/`delete_link`; `AUDIT_ACTIONS` += `share_link_audience`, `session_copied`; `decide_pickup_request` Allow also calls `put_link(…, None)`; `PickupStatus.private_repo` |
| `apps/backend/core/services/session_pickup.py` | `retire` also clears `shareLink` + lookup row; `RepoInfo`, `CommitRequest.repo`, manifest `repoInfo`, `pickupSnapshot.privateFolder`; `ready_manifest` and `downloads` helpers shared by `request_pickup` and copies |
| `apps/backend/core/services/session_links.py` | Create: `resolve`, `open_page`, `list_events`, `create_copy`, `redeem`, the redaction worker |
| `apps/backend/tests/unit/services/test_local_session_controls.py` | Drop the old share-link tests; re-point the private-send and growth tests at the one link |
| `apps/backend/routers/messaging.py` | Remove `share-links`, `shared-sessions`, `PUT …/pickup`; add `/link`, `/public/session-links/*`, `/session-links/copies`, `/public/session-copies/redeem` |
| `apps/infra/lib/stacks/api-stack.ts` | `ANY /api/v1/public/{proxy+}` route with its own throttle |
| `apps/desktop/src-tauri/src/local_sessions/snapshot.rs` | `repo_info` + anonymous probe; `repo` in the commit body |
| `apps/desktop/src-tauri/src/session_pickup.rs` | Make `Download`/`Downloads`/helpers `pub(crate)`; extract `downloads_ok`, `fetch_downloads`, `exclude_worktrees` |
| `apps/desktop/src-tauri/src/local_sessions/plugin.rs`, `local_sessions.rs` | `install_now`; `pub(crate) mod plugin` |
| `apps/desktop/src-tauri/src/session_continue.rs` | Create: the `continue` flow |
| `apps/desktop/src-tauri/src/cli.rs`, `lib.rs` | `continue` subcommand; `mod session_continue` |
| `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/{register.tsx,state.ts,band.test.ts}`, `types/index.d.ts` | "Continued from" band |
| `packages/shared/src/messaging/{types.ts,client.ts,sessionControls.ts}`, `index.ts` | Link client, audit lines, `LocalSessionPickup.privateRepo`; drop the share-link and pick-up switch APIs |
| `apps/frontend/src/components/messaging/SessionShareLink.tsx` (+test) | Create; delete `SessionShareLinks.tsx` (+test) |
| `apps/frontend/src/components/messaging/SessionDetails.tsx` (+test) | Access: Share control, copies list, no pick-up switch |
| `apps/frontend/src/app/s/page.tsx` (+test), `src/lib/returnTo.ts` (+test), `src/app/onboarding/page.tsx` | Public link page, CLI confirm, onboarding `returnTo`; delete `src/app/chat/shared/page.tsx` |

---

### Task 1: Server-side redactor

**Files:**
- Create: `apps/backend/core/services/session_redact.py`
- Test: `apps/backend/tests/unit/services/test_session_redact.py`

**Interfaces:**
- Consumes: `marketplace_safety._PATTERNS` (`marketplace_safety.py:44`; the bytes regexes named `hardcoded-secret`, `aws-secret-key`, `provider-token` at `:80-115`).
- Produces: `MARK = "[redacted]"`; `PROJECT = "<project>"`; `redact(text: str, project: str | None = None) -> str`; `redact_value(value: Any, project: str | None = None) -> Any` (recursive over `dict` values, `list`, `str`; dict keys and non-strings unchanged). `project` is a private repo's top-level folder name: as a path component it becomes `<project>` (controller ruling, Global Constraints).

- [ ] **Step 1: Write the failing test**

```python
"""session_redact: what an outsider's copy or event page never shows (spec §3.4)."""

import pytest

from core.services.session_redact import MARK, PROJECT, redact, redact_value


@pytest.mark.parametrize(
    "secret",
    [
        'api_key = "abcdefghijklmnop1234"',  # marketplace hardcoded-secret
        "AKIAABCDEFGHIJKLMNOP",  # aws-secret-key
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",  # provider-token
        "ghp_abcdefghijklmnopqrstuvwx",
        "sk_live_abcdefghijklmnop",  # Stripe
        "rk_test_abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",  # JWT
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345",  # Google
    ],
)
def test_each_secret_shape_is_replaced(secret):
    out = redact(f"before {secret} after")
    assert secret not in out and MARK in out and out.startswith("before ")


def test_pem_block_env_line_bearer_and_url_credentials():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    assert redact(f"x\n{pem}\ny") == f"x\n{MARK}\ny"
    assert redact("export DATABASE_PASSWORD=hunter2") == f"export DATABASE_PASSWORD={MARK}"
    assert redact("STRIPE_SECRET_KEY = abc") == f"STRIPE_SECRET_KEY = {MARK}"
    assert redact("Authorization: Bearer abc.def") == f"Authorization: Bearer {MARK}"
    assert redact("git clone https://bob:pa55@github.com/o/r") == f"git clone https://{MARK}@github.com/o/r"


def test_home_directories_become_tilde():
    assert redact("/Users/alice/code/app and /home/bob/x") == "~/code/app and ~/x"


def test_plain_text_is_untouched():
    text = "Ran 12 tests in apps/backend; the token budget is 4000; KEY points to the map."
    assert redact(text) == text


def test_redact_value_walks_nested_structures_but_not_keys():
    value = {"cwd": "/Users/alice/app", "tool": {"input": ["ghp_abcdefghijklmnopqrstuvwx", 3, None]}, "n": 1}
    assert redact_value(value) == {"cwd": "~/app", "tool": {"input": [MARK, 3, None]}, "n": 1}


def test_a_private_projects_folder_name_is_hidden_only_as_a_path_component():
    line = {"cwd": "/Users/alice/code/acme-api/src", "text": "Read ./acme-api/README.md and acme-api.rs; acme-api is fine"}
    assert redact_value(line, "acme-api") == {
        "cwd": f"~/code/{PROJECT}/src",
        "text": f"Read ./{PROJECT}/README.md and acme-api.rs; acme-api is fine",
    }
    assert redact("/home/bob/acme-api", "acme-api") == f"~/{PROJECT}"
    assert redact("/srv/acme-api.v2/x", "acme-api.v2") == f"/srv/{PROJECT}/x"  # the name is literal, not a regex
    assert redact("/Users/alice/acme-api") == "~/acme-api"  # no project: unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_redact.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.session_redact'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Best-effort secret redaction for whatever a session link shows someone outside the
session's workspace: their copy of the transcript and the link's event pages (spec
docs/superpowers/specs/2026-10-05-public-session-links.md §3.4).

Best effort by design: it catches the common shapes of a secret, not every secret. Members
are never redacted (Pick up hands teammates the snapshot as it is); a live screen never can be.
"""

from __future__ import annotations

import re
from typing import Any

from core.services.marketplace_safety import _PATTERNS

MARK = "[redacted]"
PROJECT = "<project>"

# The marketplace's bytes regexes, as str. Their flags are inline ((?ix)), so decoding keeps them.
_REUSED = ("hardcoded-secret", "aws-secret-key", "provider-token")

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MARK),
    *[(re.compile(p.regex.pattern.decode("ascii")), MARK) for p in _PATTERNS if p.name in _REUSED],
    # .env-shaped lines: keep the name, drop the value.
    (re.compile(r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*=\s*)\S.*$"), rf"\g<1>{MARK}"),
    (re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}"), MARK),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), MARK),
    (re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"), rf"\g<1>{MARK}"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), MARK),
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), rf"\g<1>{MARK}@"),
    (re.compile(r"/(?:Users|home)/[^/\s\"']+/"), "~/"),
]


def redact(text: str, project: str | None = None) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    if project:
        # A private repo's folder name, only where it is a whole path component (after a slash).
        text = re.sub(rf"(?<=/){re.escape(project)}(?=/|$|[\s\"'`:,;)\]])", PROJECT, text)
    return text


def redact_value(value: Any, project: str | None = None) -> Any:
    if isinstance(value, str):
        return redact(value, project)
    if isinstance(value, list):
        return [redact_value(v, project) for v in value]
    if isinstance(value, dict):
        return {k: redact_value(v, project) for k, v in value.items()}
    return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_redact.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_redact.py apps/backend/tests/unit/services/test_session_redact.py
git commit -m "feat(sessions): server-side redactor for outsider copies and event pages"
```

---

### Task 2: One link per session (owner side)

**Files:**
- Modify: `apps/backend/core/services/local_sessions.py:1721-1750` (AUDIT_ACTIONS), `:2043-2062` (decide_pickup_request), `:2227-2440` (replace the share-link section)
- Modify: `apps/backend/core/services/session_pickup.py:856-895` (`retire`)
- Modify: `apps/backend/routers/messaging.py:332-338, 369-397`
- Modify: `apps/backend/tests/unit/services/test_local_session_controls.py` (delete the four share-link tests at `:366-455` and the three lines at `:213-216`; re-point the growth test at `:798`)
- Test: `apps/backend/tests/unit/services/test_session_links_owner.py`

**Interfaces:**
- Consumes: `authorize_session`, `_owned_session` (kept, shrunk to its no-enrollment read, `:2289`), `audit_row`, `repo.put_session(row, previous, enrollment, extra=)`, `session_pickup.set_pickup`, `session_pickup.retire(owner, sid, row, auth=, action=)`, `encryption.encrypt/decrypt`.
- Produces:
  - `local_sessions.AUDIENCES = Literal["workspace", "anyone"]`
  - `local_sessions._share_pk(token_hash) -> str`, `_token_hash(token) -> str` (kept)
  - `local_sessions.LinkRequest {audience}`, `local_sessions.SessionLink {url: str | None, audience}`
  - `local_sessions.link_url(token) -> str`
  - `async put_link(auth, session_id, audience: str | None) -> SessionLink` (`None` = create as workspace if absent, else leave it alone)
  - `async get_link(auth, session_id) -> SessionLink` (404 `LOCAL_SESSION_NOT_FOUND` if none)
  - `async delete_link(auth, session_id) -> None`
  - META field `shareLink = {tokenHash, tokenEnc, audience, createdAt, createdBy}`; lookup row `SHARELINK#<hash>`/`META` = `{ownerId, sessionId}`
  - Routes: `PUT|GET|DELETE /api/v1/messaging/sessions/{id}/link`

- [ ] **Step 1: Write the failing test**

```python
"""The one link (spec §3.1): owner-only, one per session, the pick-up permission on Claude."""

import pytest

from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_pickup as pickup
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_session_relay import connect, registration, relay
from tests.unit.services.test_local_sessions import ALICE, BOB, MAC, OWNER

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session


async def codex_session(transport) -> str:
    await claude_session(transport)
    connect(transport, cid="conn-2", stamp="1001")
    await relay(transport).handle_message("conn-2", registration(nativeConversationId="codex-thread"))
    return repo.session_id(OWNER, MAC, "codex", "f" * 64, "codex-thread")


async def audit(sid):
    return [e.action for e in (await service.list_audit(ALICE, sid)).entries]


@pytest.mark.asyncio
async def test_owner_creates_one_link_that_is_the_pickup_permission(transport, s3):
    sid = await claude_session(transport)
    with pytest.raises(ApiProblem, match="owner"):
        await service.put_link(BOB, sid, "workspace")
    link = await service.put_link(ALICE, sid, "workspace")
    assert link.audience == "workspace" and "/s#" in link.url
    token = link.url.split("#", 1)[1]
    row = await repo.get_session(OWNER, sid)
    assert row["shareLink"]["tokenHash"] == service._token_hash(token) and "tokenEnc" in row["shareLink"]
    assert await messaging_repo._get(service._share_pk(service._token_hash(token)), "META") == {
        "pk": service._share_pk(service._token_hash(token)), "sk": "META", "ownerId": OWNER, "sessionId": sid,
    }
    assert row["pickupAllowed"] is True  # the link is the permission
    # Same link again; the audience changes in place.
    assert (await service.get_link(ALICE, sid)).url == link.url
    again = await service.put_link(ALICE, sid, "anyone")
    assert again.url == link.url and again.audience == "anyone"
    assert (await audit(sid))[-3:] == ["share_link_created", "pickup_on", "share_link_audience"]


@pytest.mark.asyncio
async def test_turning_the_link_off_deletes_it_and_retires_pickup(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "anyone")
    digest = service._token_hash(link.url.split("#", 1)[1])
    with pytest.raises(ApiProblem, match="owner"):
        await service.delete_link(BOB, sid)
    await service.delete_link(ALICE, sid)
    row = await repo.get_session(OWNER, sid)
    assert "shareLink" not in row and not row.get("pickupAllowed")
    assert await messaging_repo._get(service._share_pk(digest), "META") is None
    assert (await audit(sid))[-1] == "share_link_revoked"
    with pytest.raises(ApiProblem) as missing:
        await service.get_link(ALICE, sid)
    assert missing.value.status_code == 404
    await service.delete_link(ALICE, sid)  # a repeat is a no-op


@pytest.mark.asyncio
async def test_a_codex_link_never_touches_pickup(transport, s3):
    codex = await codex_session(transport)
    await service.put_link(ALICE, codex, "anyone")
    assert not (await repo.get_session(OWNER, codex)).get("pickupAllowed")
    await service.delete_link(ALICE, codex)
    assert "shareLink" not in await repo.get_session(OWNER, codex)


@pytest.mark.asyncio
async def test_a_paused_session_cannot_get_a_link(transport, s3):
    sid = await claude_session(transport)
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem, match="Resume sharing"):
        await service.put_link(ALICE, sid, "workspace")


@pytest.mark.asyncio
async def test_pickup_expiry_takes_the_link_with_it(transport, s3):
    sid = await claude_session(transport)
    link = await service.put_link(ALICE, sid, "workspace")
    await pickup.retire(OWNER, sid, await repo.get_session(OWNER, sid))
    assert "shareLink" not in await repo.get_session(OWNER, sid)
    assert await messaging_repo._get(service._share_pk(service._token_hash(link.url.split("#")[1])), "META") is None


@pytest.mark.asyncio
async def test_allowing_an_ask_to_pick_up_creates_a_workspace_link(transport, s3):
    sid = await claude_session(transport)
    asked = await service.request_pickup_access(BOB, sid)
    rid = (await repo.get_session(OWNER, sid))["pickupRequests"][BOB.user_id]["requestId"]
    await service.decide_pickup_request(ALICE, sid, BOB.user_id, "allow", request_id=rid)
    assert asked is not None
    assert (await service.get_link(ALICE, sid)).audience == "workspace"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_links_owner.py -v`
Expected: FAIL with `AttributeError: module 'core.services.local_sessions' has no attribute 'put_link'`

- [ ] **Step 3: Write minimal implementation**

3a. `AUDIT_ACTIONS` (`local_sessions.py:1721`): after `"share_link_revoked",` add `"share_link_audience",` and `"session_copied",`.

3b. Replace everything from the `# --- Share links: …` comment (`:2227`) to the end of the file (`list_shared_events`, `:2440`) with:

```python
# --- The one link (spec 2026-10-05-public-session-links §3.1) -------------------------------
#
# One link per session; its audience is the owner's only setting. On a Claude session the link is
# the pick-up permission. META.shareLink is the truth: the SHARELINK# lookup row only finds it.

AUDIENCES = Literal["workspace", "anyone"]


def _share_pk(token_hash: str) -> str:
    return f"SHARELINK#{token_hash}"


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LinkRequest(ContractModel):
    audience: AUDIENCES


class SessionLink(ContractModel):
    # None only when the stored ciphertext no longer decrypts (a rotated ENCRYPTION_KEY): the link
    # still opens, it just cannot be shown again; turning it off and on makes a new one.
    url: str | None
    audience: AUDIENCES


def link_url(token: str) -> str:
    from core.config import settings

    return f"{settings.FRONTEND_URL.rstrip('/')}/s#{token}"


def _link_public(link: dict) -> SessionLink:
    from core import encryption

    try:
        url = link_url(encryption.decrypt(link["tokenEnc"]))
    except ValueError:  # encryption.decrypt's only failure
        logger.warning("session link undecryptable")
        url = None
    return SessionLink(url=url, audience=link["audience"])


async def _owned_session(auth: AuthContext, session_id: str) -> dict:
    """The owner's own session. Reading and turning off skip the enrollment check on purpose: an
    owner who turned sharing off must still be able to find and kill their link."""
    row = await repo.get_session(resolve_owner_id(auth), validate_uuid(session_id, label="local session ID"))
    if row is None:
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "Local session not found.")
    if auth.user_id != row["humanUserId"]:
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can share it.")
    return row


def _capture_growth(row: dict, event: str, distinct_id: str, **props) -> None:
    """One PostHog row per sharing act, on the person who acted, naming the session's author.
    Content-free: ids and counts only, never a title, path, token or transcript line."""
    capture_product_event(
        distinct_id=distinct_id,
        event=event,
        properties={
            "workspace_id": row["ownerId"],
            "session_id": row["sessionId"],
            "provider": row["provider"],
            "author_user_id": row["humanUserId"],
            **props,
        },
    )


async def put_link(auth: AuthContext, session_id: str, audience: str | None) -> SessionLink:
    """Create the session's one link, or change its audience. ``audience`` None is Ask to pick
    up's Allow: create a workspace link if there is none, otherwise leave the link as it is."""
    import secrets

    from core import encryption
    from core.services import session_pickup
    from core.services.local_session_relay import publish_resources_changed

    table = messaging_repo._table().name
    for _ in range(6):
        row, enrollment = await authorize_session(auth, None, session_id)
        if auth.user_id != row["humanUserId"]:
            raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Only the session's owner can share it.")
        if row.get("unshared"):
            raise ApiProblem("LOCAL_SESSION_CONFLICT", "Resume sharing before sharing a link.")
        owner, sid, link = row["ownerId"], row["sessionId"], row.get("shareLink")
        if link and (audience is None or link["audience"] == audience):
            break
        if link:
            new = {**link, "audience": audience}
            extra = [{"Put": {"TableName": table, "Item": audit_row(owner, sid, auth, "share_link_audience", detail=audience)}}]
        else:
            token = secrets.token_urlsafe(32)
            new = {
                "tokenHash": _token_hash(token),
                "tokenEnc": encryption.encrypt(token),
                "audience": audience or "workspace",
                "createdAt": int(time.time()),
                "createdBy": auth.user_id,
            }
            lookup = {"pk": _share_pk(new["tokenHash"]), "sk": "META", "ownerId": owner, "sessionId": sid}
            extra = [
                {"Put": {"TableName": table, "Item": lookup}},
                {"Put": {"TableName": table, "Item": audit_row(owner, sid, auth, "share_link_created", detail=new["audience"])}},
            ]
        try:
            await repo.put_session({**row, "shareLink": new, "revision": str(uuid.uuid4())}, row, enrollment, extra=extra)
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        if not link:
            _capture_growth(row, "share_link_created", auth.user_id, audience=new["audience"])
        await publish_resources_changed(owner, row["humanUserId"])
        link = new
        break
    else:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
    # The link is the pick-up permission on a Claude session; set_pickup is a no-op while it is on.
    # Allow (audience None) has just turned it on itself, under its own request fence.
    if row["provider"] == "claude" and audience is not None:
        await session_pickup.set_pickup(auth, session_id, True)
    return _link_public(link)


async def get_link(auth: AuthContext, session_id: str) -> SessionLink:
    row = await _owned_session(auth, session_id)
    if not row.get("shareLink"):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session has no link.")
    return _link_public(row["shareLink"])


async def delete_link(auth: AuthContext, session_id: str) -> None:
    """Turn the link off: every page reads "turned off" and no new copy can be made. On a Claude
    session this retires the snapshot too, in retire's own transaction. Idempotent."""
    from core.services import session_pickup
    from core.services.local_session_relay import publish_resources_changed

    table = messaging_repo._table().name
    for _ in range(6):
        row = await _owned_session(auth, session_id)
        link = row.get("shareLink")
        if not link:
            return
        owner, sid = row["ownerId"], row["sessionId"]
        try:
            if row.get("pickupAllowed"):
                await session_pickup.retire(owner, sid, row, auth=auth, action="share_link_revoked")
                return
            await messaging_repo._transact(
                [
                    {
                        "Update": {
                            "TableName": table,
                            "Key": {"pk": row["pk"], "sk": "META"},
                            "UpdateExpression": "REMOVE shareLink SET revision = :new",
                            "ConditionExpression": "revision = :old",
                            "ExpressionAttributeValues": {":new": str(uuid.uuid4()), ":old": row["revision"]},
                        }
                    },
                    {"Delete": {"TableName": table, "Key": {"pk": _share_pk(link["tokenHash"]), "sk": "META"}}},
                    {"Put": {"TableName": table, "Item": audit_row(owner, sid, auth, "share_link_revoked")}},
                ]
            )
        except ClientError as exc:
            if not messaging_repo._is_cancelled(exc):
                raise
            continue
        await publish_resources_changed(owner, row["humanUserId"])
        return
    raise ApiProblem("LOCAL_SESSION_CONFLICT", "Session changed. Try again.")
```

3c. `decide_pickup_request` (`local_sessions.py:2061`): after the `set_pickup(...)` line insert

```python
        await put_link(auth, session_id, None)  # Allow creates the link as Workspace only (spec §3.1)
```

3d. `retire` (`session_pickup.py`, the `else:` branch): change `drop` and the expression, and delete the lookup row:

```python
        drop = ("pickupAllowed", "pickupSnapshot", "pickupEpoch", "shareLink")
        ...
                    "UpdateExpression": "REMOVE pickupAllowed, pickupSnapshot, pickupEpoch, shareLink SET revision = :new",
        ...
            _audit(owner, sid, auth, action),
        ]
        if row.get("shareLink"):
            # The link dies with its snapshot: expiry and turning the link off are one act (spec §3.1).
            items.append({"Delete": {"TableName": table, "Key": {"pk": sessions._share_pk(row["shareLink"]["tokenHash"]), "sk": "META"}}})
```

3e. `routers/messaging.py`: delete `set_local_session_pickup` (`:332-337`), the three `share-links` routes (`:369-384`) and the two `shared-sessions` routes (`:387-397`). Add:

```python
@router.put("/messaging/sessions/{session_id}/link", response_model=local_sessions.SessionLink)
async def put_local_session_link(
    session_id: str, body: local_sessions.LinkRequest, auth: AuthContext = Depends(get_current_user)
):
    """Owner-only: create the session's one link, or change who it is for."""
    return await local_sessions.put_link(auth, session_id, body.audience)


@router.get("/messaging/sessions/{session_id}/link", response_model=local_sessions.SessionLink)
async def get_local_session_link(session_id: str, auth: AuthContext = Depends(get_current_user)):
    return await local_sessions.get_link(auth, session_id)


@router.delete("/messaging/sessions/{session_id}/link", status_code=204)
async def delete_local_session_link(session_id: str, auth: AuthContext = Depends(get_current_user)):
    await local_sessions.delete_link(auth, session_id)
```

3f. `test_local_session_controls.py`:
- Delete `test_a_share_link_is_the_owners_to_make_a_frozen_snapshot_and_workspace_only`, `test_a_share_link_dies_at_its_expiry`, `test_turning_sharing_off_closes_links_but_the_owner_can_still_revoke_them` and `test_pausing_a_session_closes_its_links_too` (Task 3's `test_every_closed_door_reads_the_same` covers pausing).
- In the private-send test, delete the three `create_share_link`/`list_shared_events` lines at `:213-216`. Task 3 puts them back on the public events.
- In `test_sharing_and_join_events_land_on_the_person_who_acted` (`:798`): replace the three share-link lines with `link = await service.put_link(ALICE, sid, "anyone")`, delete the `("share_link_opened", BOB.user_id)` row from the expected list, and change the `expires_in_days == 7` assertion to `capture.call_args_list[0].kwargs["properties"]["audience"] == "anyone"`. Task 3 puts the open back. ("anyone", because Bob has no user stream in this file's harness, so a workspace link reads as gone to him.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_links_owner.py tests/unit/services/test_session_pickup.py tests/unit/services/test_pickup_requests.py tests/unit/services/test_local_session_controls.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/local_sessions.py apps/backend/core/services/session_pickup.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_links_owner.py apps/backend/tests/unit/services/test_local_session_controls.py
git commit -m "feat(sessions): one link per session, with an audience, as the pick-up permission"
```

---

### Task 3: Opening a link, its events, repoInfo, and the public throttle

**Files:**
- Create: `apps/backend/core/services/session_links.py`
- Modify: `apps/backend/core/services/session_pickup.py:196-212` (`CommitRequest`), `:713-745` (manifest, `pickupSnapshot`)
- Modify: `apps/backend/core/services/local_sessions.py:192-212, 719-735` (`PickupStatus.private_repo`)
- Modify: `apps/backend/routers/messaging.py` (two public routes)
- Modify: `apps/backend/tests/unit/services/test_local_session_controls.py` (put back the private-send and growth checks Task 2 removed)
- Modify: `apps/infra/lib/stacks/api-stack.ts:124-132, 380-407`; `apps/infra/test/api-stack.test.ts:219-238`
- Test: `apps/backend/tests/unit/services/test_session_links_public.py`

**Interfaces:**
- Consumes: Task 1 `redact`, `redact_value`, `MARK`; Task 2 `_share_pk`, `_token_hash`, `put_link`, `delete_link`; `sessions._enrolled`, `sessions.pickup_stopped_at`, `PICKUP_RETENTION_SECONDS`, `session_screen._still_a_member`, `repo.list_events`.
- Produces:
  - `session_pickup.RepoInfo {remote, branch, head, pushed, public, folder}`; `CommitRequest.repo: RepoInfo | None`; manifest key `repoInfo`; `pickupSnapshot.privateFolder` (set only when `public` is false)
  - `local_sessions.PickupStatus.private_repo: bool | None` (wire `privateRepo`; the owner's Share control reads it)
  - `session_links.GONE`, `session_links.LinkTokenRequest {token}`, `LinkEventsRequest {token, afterSeq, limit}`
  - `async session_links.resolve(auth: AuthContext | None, token: str) -> tuple[dict, dict, bool]` (row, shareLink, member)
  - `session_links._project(row) -> str | None` (the private folder to hide); `_known(row) -> bool` (a Claude session's first snapshot has landed)
  - `session_links.LinkCode {visibility: "public"|"private"|"none"|"workspace", remote?, branch?, commit?}`, `LinkMe {signedIn, member}`, `LinkPage {title, ownerFirstName, provider, state, code, continuable, me, sessionId?}`
  - `async open_page(auth, token) -> LinkPage`; `async list_events(auth, token, after_seq, limit) -> LocalSessionEventPage`
  - `async _name(user_id, *, first: bool) -> str` (never a user id)
  - Routes: `POST /api/v1/public/session-links/open`, `POST /api/v1/public/session-links/events` (optional auth)

- [ ] **Step 1: Write the failing test**

```python
"""A link's holder: the page model and the event pages (spec §3.2, §3.3)."""

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo
from core.services import local_sessions as service, session_links as links
from core.services.local_sessions import PICKUP_RETENTION_SECONDS
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_session_relay import relay
from tests.unit.services.test_local_sessions import ALICE, BOB, OWNER

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session
upload_and_commit = pickup_tests.upload_and_commit

STRANGER = AuthContext(user_id="user_elsewhere", org_id="org_other")
PUBLIC = {"remote": "github.com/acme/app", "branch": "main", "head": "a" * 40, "pushed": True, "public": True, "folder": "app"}


@pytest.fixture(autouse=True)
def names():
    with patch("core.services.messaging.human_display_name", AsyncMock(return_value="Alice Doe")):
        yield


async def link_for(transport, s3, audience="anyone", repo_info=None):
    sid = await claude_session(transport)
    url = (await service.put_link(ALICE, sid, audience)).url
    extra = {"repo": repo_info} if repo_info is not None else {}
    assert (await upload_and_commit(s3, sid, **extra)).status_code == 204
    return sid, url.split("#", 1)[1]


async def say(transport, sid, texts, after=0):
    events = [{"kind": "text", "role": "assistant", "text": t, "truncated": False, "actor": {"userId": BOB.user_id}} for t in texts]
    batch = {"type": "local.session.events", "sessionId": sid, "generation": "generation-1",
             "batchId": f"b-{after}", "afterSeq": after, "events": events}
    await relay(transport).handle_message("conn-1", batch)


@pytest.mark.asyncio
async def test_anyone_link_page_names_no_ids_and_shows_a_public_repo(transport, s3):
    sid, token = await link_for(transport, s3, repo_info=PUBLIC)
    page = (await links.open_page(None, token)).model_dump(by_alias=True)
    assert page == {
        "title": page["title"], "ownerFirstName": "Alice", "provider": "claude", "state": "live",
        "code": {"visibility": "public", "remote": "github.com/acme/app", "branch": "main", "commit": "a" * 40},
        "continuable": True, "me": {"signedIn": False, "member": False},
    }
    text = json.dumps(page)
    assert OWNER not in text and ALICE.user_id not in text and sid not in text


@pytest.mark.asyncio
async def test_a_private_repo_carries_only_the_flag(transport, s3):
    _, token = await link_for(transport, s3, repo_info={**PUBLIC, "public": False})
    page = (await links.open_page(STRANGER, token)).model_dump(by_alias=True)
    assert page["code"] == {"visibility": "private"} and "acme" not in json.dumps(page)


@pytest.mark.asyncio
async def test_unpushed_or_unknown_code_is_not_shared(transport, s3):
    _, token = await link_for(transport, s3, repo_info={**PUBLIC, "pushed": False})
    assert (await links.open_page(None, token)).code.visibility == "none"
    _, older_mac = await link_for(transport, s3)
    assert (await links.open_page(None, older_mac)).code.visibility == "none"


@pytest.mark.asyncio
async def test_a_member_gets_the_session_and_the_workspace_repo_line(transport, s3):
    sid, token = await link_for(transport, s3, audience="workspace", repo_info={**PUBLIC, "public": False})
    page = await links.open_page(BOB, token)
    # No bundle was committed, so a member sees "Code not shared" too; the flag is for outsiders.
    assert page.me.member and page.session_id == sid and page.code.visibility == "none"


@pytest.mark.asyncio
async def test_a_workspace_link_reads_as_gone_to_everyone_else(transport, s3):
    _, token = await link_for(transport, s3, audience="workspace")
    for caller in (None, STRANGER, AuthContext(user_id="user_noorg", org_id=None)):
        with pytest.raises(ApiProblem, match=links.GONE):
            await links.open_page(caller, token)
    await messaging_repo.deactivate_user_stream(OWNER, BOB.user_id)  # removed from the workspace
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(BOB, token)


@pytest.mark.asyncio
async def test_every_closed_door_reads_the_same(transport, s3):
    sid, token = await link_for(transport, s3)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, token[:-1] + ("A" if token[-1] != "A" else "B"))
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, "short")
    # An old many-links row with no META.shareLink match reads as turned off.
    old = "o" * 43
    await messaging_repo._table().put_item(Item={"pk": service._share_pk(service._token_hash(old)), "sk": "META",
                                                  "ownerId": OWNER, "sessionId": sid, "linkId": "x", "expiresAt": 2**31})
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, old)
    with patch("time.time", return_value=time.time() + PICKUP_RETENTION_SECONDS + 7200):
        with pytest.raises(ApiProblem, match=links.GONE):
            await links.open_page(None, token)
    await service.set_sharing(ALICE, sid, False)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.open_page(None, token)
    await service.set_sharing(ALICE, sid, True)
    await service.delete_link(ALICE, sid)
    with pytest.raises(ApiProblem, match=links.GONE):
        await links.list_events(None, token, 0, 50)


@pytest.mark.asyncio
async def test_outsider_events_are_redacted_and_carry_no_actor(transport, s3):
    sid, token = await link_for(transport, s3)
    await say(transport, sid, ["key ghp_abcdefghijklmnopqrstuvwx in /Users/alice/app", "ok"])
    page = (await links.list_events(STRANGER, token, 0, 50)).model_dump(by_alias=True)
    assert [e["text"] for e in page["events"]] == ["key [redacted] in ~/app", "ok"]
    assert all("actor" not in e and "originRequestId" not in e for e in page["events"])
    member = (await links.list_events(BOB, token, 0, 50)).model_dump(by_alias=True)
    assert member["events"][0]["text"].startswith("key ghp_") and "actor" in member["events"][0]


@pytest.mark.asyncio
async def test_a_private_repos_folder_is_hidden_from_outsiders_only(transport, s3):
    sid, token = await link_for(transport, s3, repo_info={**PUBLIC, "public": False})
    await say(transport, sid, ["edited /Users/alice/app/src/a.py"])
    assert [e.text for e in (await links.list_events(STRANGER, token, 0, 50)).events] == ["edited ~/<project>/src/a.py"]
    assert (await links.list_events(ALICE, token, 0, 50)).events[0].text == "edited /Users/alice/app/src/a.py"
    assert (await repo.get_session(OWNER, sid))["pickupSnapshot"]["privateFolder"] == "app"
    assert (await service.get_session(ALICE, sid)).pickup.private_repo is True


@pytest.mark.asyncio
async def test_outsiders_see_no_events_before_the_first_snapshot(transport, s3):
    # Until the Mac commits, nobody knows whether the folder name is private.
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, "anyone")).url.split("#", 1)[1]
    await say(transport, sid, ["in /Users/alice/app"])
    assert (await links.list_events(STRANGER, token, 0, 50)).events == []
    assert len((await links.list_events(ALICE, token, 0, 50)).events) == 1


@pytest.mark.asyncio
async def test_an_event_that_grows_past_the_envelope_falls_back_to_one_line(transport, s3):
    sid, token = await link_for(transport, s3)
    row = {"pk": repo.session_pk(OWNER, sid), "sk": f"EVENT#{99:020d}", "seq": 99, "kind": "text", "role": "assistant",
           "truncated": False, "text": "://a:b@ " * 2000}
    await messaging_repo._table().put_item(Item=row)
    events = (await links.list_events(None, token, 98, 10)).events
    assert events[0].text == "[redacted]" and events[0].truncated is True


@pytest.mark.asyncio
async def test_owner_name_never_leaks_a_user_id(transport, s3):
    _, token = await link_for(transport, s3)
    with patch("core.services.messaging.human_display_name", AsyncMock(return_value=ALICE.user_id)):
        assert (await links.open_page(None, token)).owner_first_name == "Someone"


@pytest.mark.asyncio
async def test_the_commit_stores_repo_info_on_the_manifest(transport, s3):
    sid, _ = await link_for(transport, s3, repo_info=PUBLIC)
    manifest = await messaging_repo._get(repo.session_pk(OWNER, sid), "PICKUP")
    assert manifest["repoInfo"] == PUBLIC
```

`messaging_repo.deactivate_user_stream` (`messaging_repo.py:2086`) is the removal path that `_still_a_member` reads.

In `test_local_session_controls.py`, put back what Task 2 took out (import `session_links` from `core.services`):
- Private-send test, where the three lines were: `token = (await service.put_link(ALICE, sid, "workspace")).url.split("#", 1)[1]` and `assert "Private\nbody" not in json.dumps((await session_links.list_events(ALICE, token, 0, 100)).model_dump(by_alias=True))`.
- Growth test, after `put_link`: `token = link.url.split("#", 1)[1]`, `await session_links.open_page(BOB, token)`, `await session_links.list_events(BOB, token, 0, 50)  # paging is not another open`; restore `("share_link_opened", BOB.user_id)` as the second expected row.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_links_public.py -v`
Expected: FAIL with `ImportError: cannot import name 'session_links'`

- [ ] **Step 3: Write minimal implementation**

3a. `session_pickup.py`, above `CommitRequest`:

```python
class RepoInfo(ContractModel):
    """Where the session's code lives, as the author's Mac sees it (spec §3.3). The Mac is the
    owner's own device, so a wrong answer only mis-describes the owner's own repository."""

    remote: Annotated[str, Field(min_length=1, max_length=256)]
    branch: Annotated[str, Field(max_length=256)] | None = None
    head: GitSha
    pushed: bool
    public: bool
    # The repository's top-level folder name: hidden from outsiders when the repo is private.
    # Null only for a repository at `/`.
    folder: Annotated[str, Field(min_length=1, max_length=255)] | None = None
```

In `CommitRequest`, add `repo: RepoInfo | None = None` (last field). In `commit()`'s `manifest = {...}`, add `"repoInfo": request.repo.model_dump(by_alias=True) if request.repo else None,`. After `snapshot = {"at": now, ...}` add:

```python
        if request.repo and not request.repo.public:
            snapshot["privateFolder"] = request.repo.folder or ""  # read by session_links without a manifest read
```

`local_sessions.py`: in `PickupStatus` add `private_repo: bool | None = None` and `"private_repo"` to its `_drop_none` names; in `_pickup_public` add `"privateRepo": True if "privateFolder" in snapshot else None,`. The folder name itself stays server-side.

ponytail: an older Mac sends no `repo`, so its private folder names are not hidden; the deploy order (Global Constraints) keeps that window short.

3b. Create `session_links.py`:

```python
"""What a session link's holder can do (spec docs/superpowers/specs/2026-10-05-public-session-links.md
§3.2-§3.5): open its page, read its events, and take a copy to continue. The owner's side
(create, audience, turn off) is local_sessions.put_link/get_link/delete_link.

Anyone holding an "anyone" link reaches open and events with no sign-in. Nothing here returns an
owner, user, workspace or session id, or a path, to someone outside the session's workspace."""

from __future__ import annotations

import logging
import time
from typing import Annotated, Literal

from pydantic import Field, model_serializer

from core.auth import AuthContext
from core.repositories import local_sessions_repo as repo, messaging_repo as store
from core.services import local_sessions as sessions, session_pickup as pickup
from core.services.local_sessions import PICKUP_RETENTION_SECONDS, _drop_none
from core.services.messaging import ApiProblem, ContractModel
from core.services.session_redact import MARK, redact, redact_value
from core.services.session_screen import _still_a_member

logger = logging.getLogger(__name__)

GONE = "This link was turned off or is not for you."
Token = Annotated[str, Field(min_length=16, max_length=128)]


def _gone() -> ApiProblem:
    return ApiProblem("LOCAL_SESSION_NOT_FOUND", GONE)


class LinkTokenRequest(ContractModel):
    """The token travels in a body, never a path or query: those land in access logs."""

    token: Token


class LinkEventsRequest(LinkTokenRequest):
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)


async def _member(auth: AuthContext | None, row: dict) -> bool:
    if auth is None or auth.org_id != row["ownerId"]:
        return False
    try:
        await _still_a_member(row, auth.user_id)
    except ApiProblem:
        return False
    return True


async def resolve(auth: AuthContext | None, token: str) -> tuple[dict, dict, bool]:
    """(META row, its shareLink, whether the caller is a current member). Every refusal reads the
    same: no row, a hash META does not hold, paused, the Mac no longer shares, past the snapshot
    retention, or a workspace link for someone outside it. The route bounds the token's length."""
    digest = sessions._token_hash(token)
    lookup = await store._get(sessions._share_pk(digest), "META")
    row = await repo.get_session(lookup["ownerId"], lookup["sessionId"]) if lookup else None
    link = (row or {}).get("shareLink") or {}
    # Plain !=: the lookup row was found by this hash, so the comparison reveals nothing new.
    if row is None or row.get("unshared") or link.get("tokenHash") != digest:
        raise _gone()
    try:
        await sessions._enrolled(row["ownerId"], row["humanUserId"], row["deviceId"])
    except ApiProblem as exc:
        raise _gone() from exc
    stopped = sessions.pickup_stopped_at(row)
    if stopped is not None and int(time.time()) >= stopped + PICKUP_RETENTION_SECONDS:
        raise _gone()
    member = await _member(auth, row)
    if link["audience"] != "anyone" and not member:
        raise _gone()
    return row, link, member


async def _name(user_id: str, *, first: bool) -> str:
    """A display name, never an id: human_display_name falls back to the Clerk user id."""
    from core.services.messaging import human_display_name

    try:
        name = (await human_display_name(user_id)).strip()
    except Exception:
        name = ""
    if not name or name == user_id:
        return "Someone"
    return name.split()[0][:40] if first else name[:80]


class LinkCode(ContractModel):
    visibility: Literal["public", "private", "none", "workspace"]
    remote: str | None = None
    branch: str | None = None
    commit: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("remote", "branch", "commit"))


class LinkMe(ContractModel):
    signed_in: bool
    member: bool


class LinkPage(ContractModel):
    title: str | None
    owner_first_name: str
    provider: Literal["claude", "codex"]
    state: Literal["live", "ended", "offline"]
    code: LinkCode
    continuable: bool
    me: LinkMe
    # Members only: their in-app paths (Watch, Pick up) need it.
    session_id: str | None = None

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("session_id",))


def code_line(row: dict, manifest: dict | None, member: bool) -> LinkCode:
    """Spec §3.3's table. A private repo carries the flag and nothing else."""
    if member:
        snapshot = row.get("pickupSnapshot") or {}
        if snapshot.get("repo"):
            return LinkCode(visibility="workspace", remote=snapshot["repo"], branch=snapshot.get("branch"))
        return LinkCode(visibility="none")
    info = (manifest or {}).get("repoInfo")
    if not info or not info.get("pushed"):
        return LinkCode(visibility="none")
    if not info.get("public"):
        return LinkCode(visibility="private")
    return LinkCode(visibility="public", remote=info["remote"], branch=info.get("branch"), commit=info["head"])


def _state(row: dict) -> str:
    if row.get("endedAt") is not None:
        return "ended"
    return "live" if int(row["onlineUntil"]) > int(time.time()) else "offline"


def _known(row: dict) -> bool:
    """A Claude session's first snapshot has landed, so its repo's privacy is known (Codex has none)."""
    return row["provider"] != "claude" or (row.get("pickupSnapshot") or {}).get("state") == "ready"


def _project(row: dict) -> str | None:
    """The private repo's folder name, hidden from outsiders (controller ruling 2026-10-05)."""
    return (row.get("pickupSnapshot") or {}).get("privateFolder") or None


def _title(row: dict, member: bool) -> str | None:
    title = row.get("displayTitle") or row.get("title")
    if member or title is None:
        return title
    return redact(title, _project(row)) if _known(row) else None


async def open_page(auth: AuthContext | None, token: str) -> LinkPage:
    row, _, member = await resolve(auth, token)
    manifest = await store._get(row["pk"], "PICKUP") if row.get("pickupAllowed") else None
    continuable = (
        row["provider"] == "claude"
        and manifest is not None
        and (row.get("pickupSnapshot") or {}).get("state") == "ready"
        and not pickup._expired(row)
    )
    if auth is not None:
        sessions._capture_growth(row, "share_link_opened", auth.user_id, member=member)
    return LinkPage(
        title=_title(row, member),
        owner_first_name=await _name(row["humanUserId"], first=True),
        provider=row["provider"],
        state=_state(row),
        code=code_line(row, manifest, member),
        continuable=continuable,
        me=LinkMe(signed_in=auth is not None, member=member),
        session_id=row["sessionId"] if member else None,
    )


def _event(stored: dict, member: bool, project: str | None) -> sessions.LocalSessionEvent:
    if member:
        return sessions._public(sessions.LocalSessionEvent, stored)
    data = {k: v for k, v in stored.items() if k not in ("actor", "originRequestId")}
    for key in ("text", "tool"):
        if key in data:
            data[key] = redact_value(data[key], project)
    try:
        return sessions._public(sessions.LocalSessionEvent, data)
    except ValueError:  # redaction grew it past the 16 KiB envelope: one line in its place
        return sessions.LocalSessionEvent(seq=stored["seq"], kind=stored["kind"], role=stored["role"], text=MARK, truncated=True)


async def list_events(auth: AuthContext | None, token: str, after_seq: int, limit: int) -> sessions.LocalSessionEventPage:
    row, _, member = await resolve(auth, token)
    if not member and not _known(row):
        return sessions.LocalSessionEventPage(events=[], next_after_seq=None)  # Review Focus 6
    rows, next_seq = await repo.list_events(row["ownerId"], row["sessionId"], after_seq, limit)
    project = None if member else _project(row)
    return sessions.LocalSessionEventPage(events=[_event(r, member, project) for r in rows], next_after_seq=next_seq)
```

3c. `routers/messaging.py`: import `get_optional_user` from `core.auth` and `session_links` from `core.services`, then add:

```python
# Public: an "anyone" link opens with no sign-in. POST so the token rides in the body. These sit
# under /public/, which API Gateway throttles on its own bucket (api-stack.ts).
@router.post("/public/session-links/open", response_model=session_links.LinkPage)
async def open_session_link(
    body: session_links.LinkTokenRequest, auth: AuthContext | None = Depends(get_optional_user)
):
    return await session_links.open_page(auth, body.token)


@router.post("/public/session-links/events", response_model=local_sessions.LocalSessionEventPage)
async def list_session_link_events(
    body: session_links.LinkEventsRequest, auth: AuthContext | None = Depends(get_optional_user)
):
    return await session_links.list_events(auth, body.token, body.after_seq, body.limit)
```

3d. `api-stack.ts`: below `SLACK_THROTTLE` add

```ts
// Session links open with no sign-in (spec 2026-10-05-public-session-links §6): their own bucket,
// so an unauthenticated flood never eats the stage-wide budget, and the reverse.
const PUBLIC_ROUTE_KEY = "ANY /api/v1/public/{proxy+}";
const PUBLIC_THROTTLE: Record<string, { burstLimit: number; rateLimit: number }> = {
  dev: { burstLimit: 20, rateLimit: 10 },
  prod: { burstLimit: 100, rateLimit: 50 },
};
```

Below `HttpSlackEventsRoute` add

```ts
    new apigatewayv2.CfnRoute(this, "HttpPublicRoute", {
      apiId: httpApi.ref,
      routeKey: PUBLIC_ROUTE_KEY,
      target: `integrations/${httpIntegration.ref}`,
    });
```

In `routeSettings` add

```ts
        [PUBLIC_ROUTE_KEY]: {
          ThrottlingBurstLimit: (PUBLIC_THROTTLE[env] ?? PUBLIC_THROTTLE.dev).burstLimit,
          ThrottlingRateLimit: (PUBLIC_THROTTLE[env] ?? PUBLIC_THROTTLE.dev).rateLimit,
        },
```

In `api-stack.test.ts`, after the Slack describe block:

```ts
describe("ApiStack — public session-link routes have their own throttle bucket", () => {
  test("route and stage settings exist", () => {
    API_TEMPLATE.hasResourceProperties("AWS::ApiGatewayV2::Route", { RouteKey: "ANY /api/v1/public/{proxy+}" });
    API_TEMPLATE.hasResourceProperties("AWS::ApiGatewayV2::Stage", {
      RouteSettings: Match.objectLike({
        "ANY /api/v1/public/{proxy+}": Match.objectLike({ ThrottlingBurstLimit: 20, ThrottlingRateLimit: 10 }),
      }),
    });
  });
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_links_public.py tests/unit/services/test_session_pickup.py tests/unit/services/test_local_session_controls.py -v`
Expected: PASS
Run: `cd apps/infra && npx jest test/api-stack.test.ts -t "public session-link"`
Expected: PASS
Run: `cd apps/infra && npx cdk synth > /dev/null && git status --short ../../.github/workflows/deploy.yml`
Expected: synth exits 0. If `deploy.yml` changed, stage it with this task.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_links.py apps/backend/core/services/session_pickup.py apps/backend/core/services/local_sessions.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_links_public.py apps/backend/tests/unit/services/test_local_session_controls.py apps/infra/lib/stacks/api-stack.ts apps/infra/test/api-stack.test.ts
git add .github/workflows/deploy.yml 2>/dev/null || true
git commit -m "feat(sessions): open a session link and read its events, redacted for outsiders"
```

---

### Task 4: Copies, the redaction worker, and redeem with a verifier

**Files:**
- Modify: `apps/backend/core/services/session_links.py`
- Modify: `apps/backend/core/services/session_pickup.py:406-424` (extract `ready_manifest`), `:459-488` (extract `downloads`)
- Modify: `apps/backend/routers/messaging.py`
- Test: `apps/backend/tests/unit/services/test_session_copies.py`

**Interfaces:**
- Consumes: Task 3 `resolve`, `_name`, `_title`; `session_pickup._s3`, `_bucket`, `session_prefix`, `_part_key`, `_key`, `_expired`, `Download`, `TranscriptDownload`, `Downloads`, `URL_SECONDS`; Task 1 `redact`, `redact_value`.
- Produces:
  - `session_pickup.ready_manifest(row) -> dict` (async; the checks `request_pickup` made inline)
  - `session_pickup.downloads(row, manifest) -> Downloads` (sync; the presigned URLs `_ticket` built inline)
  - `session_links.CopyRequest {token, challenge}`, `CopyCode {code}`, `RedeemRequest {code, verifier}`
  - `session_links.CopyCodeMode {mode: "clone"|"bundle"|"none", remote?, commit?, snapshot?, sessionId?}`
  - `session_links.CopyTicket {state: "ready", title, from, conversationId, claudeVersion, cwdRelative, downloads: Downloads, code: CopyCodeMode}`; `Preparing {state: "preparing"}`
  - `async create_copy(auth, token, challenge) -> CopyCode`; `async redeem(code, verifier) -> CopyTicket | Preparing`
  - `challenge_of(verifier: str) -> str` = base64url-nopad(sha256(verifier))
  - Row `SESSIONCOPY#<codeHash>`/`META`
  - Routes: `POST /api/v1/session-links/copies` (`get_human_session`), `POST /api/v1/public/session-copies/redeem` (no auth)

- [ ] **Step 1: Write the failing test**

```python
"""Copies (spec §3.5): made when Continue is pressed, redacted for outsiders, redeemed by the CLI."""

import gzip
import io
import json
import tarfile
import time
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from core.auth import AuthContext
from core.repositories import messaging_repo
from core.services import local_sessions as service, session_links as links
from core.services.messaging import ApiProblem
from tests.unit.services import test_session_pickup as pickup_tests
from tests.unit.services.test_local_sessions import ALICE, BOB

db = pickup_tests.db
transport = pickup_tests.transport
s3 = pickup_tests.s3
_bob_is_a_member = pickup_tests._bob_is_a_member
claude_session = pickup_tests.claude_session
upload_and_commit = pickup_tests.upload_and_commit
BUCKET = pickup_tests.BUCKET

STRANGER = AuthContext(user_id="user_elsewhere", org_id="org_other")
VERIFIER = "v" * 43
PUBLIC = {"remote": "github.com/acme/app", "branch": "main", "head": "a" * 40, "pushed": True, "public": True, "folder": "app"}
SECRET_LINE = b'{"cwd":"/Users/alice/app","message":"token ghp_abcdefghijklmnopqrstuvwx"}\n'


@pytest.fixture(autouse=True)
def names():
    with patch("core.services.messaging.human_display_name", AsyncMock(return_value="Sam Lee")):
        yield


@pytest.fixture
def started():
    """Background preparation, run by the test when it chooses."""
    items = []
    with patch.object(links, "_start", items.append):
        yield items


def sidecars_blob() -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in (("subagents/a.jsonl", SECRET_LINE), ("image.png", b"\x89PNG\x00\xff")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


async def shared(transport, s3, audience="anyone", repo_info=PUBLIC, chunks=(SECRET_LINE,), **commit):
    sid = await claude_session(transport)
    token = (await service.put_link(ALICE, sid, audience)).url.split("#", 1)[1]
    assert (await upload_and_commit(s3, sid, chunks=chunks, repo=repo_info, **commit)).status_code == 204
    return sid, token


def fetch(s3, url: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=urlparse(url).path.lstrip("/"))["Body"].read()


@pytest.mark.asyncio
async def test_an_outsider_copy_is_redacted_and_clones_a_public_repo(transport, s3, started):
    sid, token = await shared(transport, s3, sidecars_blob=sidecars_blob())
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    assert (await links.redeem(code, VERIFIER)).state == "preparing"
    await links._prepare(started[0])
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["from"] == "Sam"  # the names fixture answers "Sam Lee" for everyone
    assert ticket["code"] == {"mode": "clone", "remote": "github.com/acme/app", "commit": "a" * 40}
    body = gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))
    assert b"ghp_" not in body and b"[redacted]" in body and b'"~/app"' in body
    assert "/copies/" in ticket["downloads"]["transcript"][0]["url"]
    with tarfile.open(fileobj=io.BytesIO(fetch(s3, ticket["downloads"]["sidecars"]["url"])), mode="r:gz") as tar:
        assert tar.getnames() == ["subagents/a.jsonl"]  # the binary entry is dropped
        assert b"ghp_" not in tar.extractfile("subagents/a.jsonl").read()
    text = json.dumps(ticket)
    assert sid not in text and ALICE.user_id not in text
    entries = (await service.list_audit(ALICE, sid)).entries
    assert (entries[-1].action, entries[-1].detail, entries[-1].subject_user_id) == ("session_copied", "Sam Lee", STRANGER.user_id)


@pytest.mark.asyncio
async def test_a_private_repo_copy_is_conversation_only_and_names_nothing(transport, s3, started):
    _, token = await shared(transport, s3, repo_info={**PUBLIC, "public": False})
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["code"] == {"mode": "none"} and ticket["cwdRelative"] == ""
    assert "acme" not in json.dumps(ticket) and "main" not in json.dumps(ticket["code"])
    body = gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))
    assert json.loads(body)["cwd"] == "~/<project>"  # the private folder name stays on the Mac's side


@pytest.mark.asyncio
async def test_a_member_copy_is_ready_at_once_with_the_bundle(transport, s3, started):
    sid, token = await shared(transport, s3, audience="workspace", code_blob=b"bundle-bytes")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    assert started == []
    ticket = (await links.redeem(code, VERIFIER)).model_dump(by_alias=True)
    assert ticket["code"]["mode"] == "bundle" and ticket["code"]["sessionId"] == sid
    assert ticket["downloads"]["code"] is not None and "/copies/" not in ticket["downloads"]["transcript"][0]["url"]
    assert b"ghp_" in gzip.decompress(fetch(s3, ticket["downloads"]["transcript"][0]["url"]))  # members: unredacted


@pytest.mark.asyncio
async def test_a_line_split_across_two_parts_is_redacted_whole(transport, s3, started):
    first, second = SECRET_LINE[:30], SECRET_LINE[30:]
    _, token = await shared(transport, s3, chunks=(first, second))
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    await links._prepare(started[0])
    ticket = await links.redeem(code, VERIFIER)
    joined = b"".join(gzip.decompress(fetch(s3, p.url)) for p in ticket.downloads.transcript)
    assert b"ghp_" not in joined and json.loads(joined)["cwd"] == "~/app"


@pytest.mark.asyncio
async def test_redeem_needs_the_verifier_and_the_window(transport, s3, started):
    _, token = await shared(transport, s3, audience="workspace")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    with pytest.raises(ApiProblem) as wrong:
        await links.redeem(code, "w" * 43)
    assert wrong.value.status_code == 404
    with patch("time.time", return_value=time.time() + links.CODE_WINDOW_S + 1):
        with pytest.raises(ApiProblem):
            await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_turning_the_link_off_refuses_a_pending_redeem(transport, s3, started):
    sid, token = await shared(transport, s3, audience="workspace")
    code = (await links.create_copy(BOB, token, links.challenge_of(VERIFIER))).code
    await service.delete_link(ALICE, sid)
    with pytest.raises(ApiProblem):
        await links.redeem(code, VERIFIER)


@pytest.mark.asyncio
async def test_a_stalled_preparation_restarts(transport, s3, started):
    _, token = await shared(transport, s3)
    code = (await links.create_copy(STRANGER, token, links.challenge_of(VERIFIER))).code
    with patch("time.time", return_value=time.time() + links.RESTART_AFTER_S + 1):
        assert (await links.redeem(code, VERIFIER)).state == "preparing"
    assert len(started) == 2


@pytest.mark.asyncio
async def test_continue_needs_the_takers_own_workspace(transport, s3):
    _, token = await shared(transport, s3)
    with pytest.raises(HTTPException) as no_org:
        await links.create_copy(AuthContext(user_id="user_new", org_id=None), token, links.challenge_of(VERIFIER))
    assert no_org.value.status_code == 409
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_copies.py -v`
Expected: FAIL with `AttributeError: module 'core.services.session_links' has no attribute 'create_copy'`

- [ ] **Step 3: Write minimal implementation**

3a. `session_pickup.py`: extract the checks from `request_pickup` (`:411-421`):

```python
async def ready_manifest(row: dict) -> dict:
    """The committed manifest a pick-up or a copy may use, or the reason it may not."""
    if not row.get("pickupAllowed"):
        raise ApiProblem("LOCAL_SESSION_FORBIDDEN", "Pick-up is off for this session.")
    if _expired(row):
        raise ApiProblem("LOCAL_SESSION_NOT_FOUND", "This session's snapshot has expired.")
    manifest = await store._get(row["pk"], "PICKUP")
    if (row.get("pickupSnapshot") or {}).get("state") != "ready" or manifest is None:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "The snapshot is not ready yet.")
    if manifest.get("pickupEpoch", row["pickupEpoch"]) != row["pickupEpoch"]:
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "Snapshot permission changed. Try again.")
    return manifest
```

`request_pickup` then calls `manifest = await ready_manifest(row)` in place of those lines. Its error messages stay the same.

Extract the `Downloads(...)` expression of `_ticket` into `def downloads(row: dict, manifest: dict) -> Downloads` (it builds its own `url()` signer from `_s3()`/`_bucket()`); `_ticket` passes `downloads=downloads(row, manifest)`. A member's copy reuses it below.

3b. Append to `session_links.py` (add `import asyncio, base64, gzip, hashlib, hmac, io, json, secrets, tarfile, tempfile, uuid` and `from core.auth import resolve_owner_id`, `from core.dynamodb import run_in_thread`):

```python
# --- Copies (spec §3.5) ------------------------------------------------------------------------

COPY_TTL_S = 3600
CODE_WINDOW_S = 600
RESTART_AFTER_S = 300
_redacting = asyncio.Semaphore(1)  # one redaction at a time per backend task
_running: set[asyncio.Task] = set()  # held so a running preparation is not garbage-collected

Challenge = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]


class CopyRequest(ContractModel):
    token: Token
    challenge: Challenge


class CopyCode(ContractModel):
    code: str


class RedeemRequest(ContractModel):
    code: Token
    verifier: Annotated[str, Field(min_length=43, max_length=128)]


class CopyCodeMode(ContractModel):
    mode: Literal["clone", "bundle", "none"]
    remote: str | None = None
    commit: str | None = None  # clone: the commit to check out
    snapshot: str | None = None  # bundle: the snapshot commit
    session_id: str | None = None  # bundle: names refs/lightbulb/snap/<id> inside the bundle (members only)

    @model_serializer(mode="wrap")
    def wire(self, handler, info):
        return _drop_none(self, handler(self), info, ("remote", "commit", "snapshot", "session_id"))


class CopyTicket(ContractModel):
    """Not a PickupTicket: that one names the session, its human and the folder."""

    state: Literal["ready"] = "ready"
    title: str | None
    from_: str = Field(alias="from")
    conversation_id: str
    claude_version: str
    cwd_relative: str
    downloads: pickup.Downloads
    code: CopyCodeMode


class Preparing(ContractModel):
    state: Literal["preparing"] = "preparing"


def challenge_of(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _copy_pk(code: str) -> str:
    return f"SESSIONCOPY#{sessions._token_hash(code)}"


def _code_mode(manifest: dict, member: bool) -> str:
    if member:
        return "bundle" if manifest.get("code") else "none"
    info = manifest.get("repoInfo") or {}
    return "clone" if info.get("public") and info.get("pushed") else "none"


async def create_copy(auth: AuthContext, token: str, challenge: str) -> CopyCode:
    resolve_owner_id(auth)  # the taker's own workspace: the free one sign-up gives (409 without)
    row, link, member = await resolve(auth, token)
    if row["provider"] != "claude":
        raise ApiProblem("LOCAL_SESSION_UNSUPPORTED", "Only Claude Code sessions can be continued.")
    manifest = await pickup.ready_manifest(row)
    code, now = secrets.token_urlsafe(32), int(time.time())
    item = {
        "pk": _copy_pk(code),
        "sk": "META",
        "ownerId": row["ownerId"],
        "sessionId": row["sessionId"],
        "copyId": uuid.uuid4().hex,
        "userId": auth.user_id,
        "member": member,
        "challenge": challenge,
        "linkHash": link["tokenHash"],
        "epoch": row["pickupEpoch"],
        "codeMode": _code_mode(manifest, member),
        "state": "ready" if member else "preparing",
        "createdAt": now,
        "startedAt": now,
        "expiresAt": now + COPY_TTL_S,
    }
    table = store._table().name
    audit = sessions.audit_row(
        row["ownerId"], row["sessionId"], auth, "session_copied",
        detail=await _name(auth.user_id, first=False), subject_user_id=auth.user_id,
    )
    await store._transact([{"Put": {"TableName": table, "Item": item}}, {"Put": {"TableName": table, "Item": audit}}])
    if not member:
        _start(item)
    sessions._capture_growth(row, "session_copied", auth.user_id, member=member)
    return CopyCode(code=code)


def _start(item: dict) -> None:
    task = asyncio.get_running_loop().create_task(_prepare(item))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _settle(item: dict, values: dict) -> None:
    """Write the outcome only if this run is still the copy's current one (a restart moves startedAt)."""
    names = {f"#{k}": k for k in values}
    await run_in_thread(
        store._table().update_item,
        Key={"pk": item["pk"], "sk": "META"},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in values),
        ConditionExpression="startedAt = :started",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**{f":{k}": v for k, v in values.items()}, ":started": item["startedAt"]},
    )


async def _prepare(item: dict) -> None:
    async with _redacting:
        try:
            row = await repo.get_session(item["ownerId"], item["sessionId"])
            manifest = await store._get(row["pk"], "PICKUP") if row else None
            if not row or not manifest or row.get("pickupEpoch") != item["epoch"]:
                raise RuntimeError("snapshot gone")
            info = manifest.get("repoInfo") or {}
            project = info.get("folder") if info and not info.get("public") else None
            out = await run_in_thread(_redact_copy, row, manifest, item["copyId"], project)
            out.update(
                state="ready",
                readyAt=int(time.time()),
                conversationId=manifest["conversationId"],
                claudeVersion=manifest["claudeVersion"],
                cwdRelative=manifest["cwdRelative"] if item["codeMode"] == "clone" else "",
                repo=manifest.get("repoInfo") if item["codeMode"] == "clone" else None,
            )
            await _settle(item, out)
        except Exception as exc:
            logger.warning("session copy failed: %s", type(exc).__name__, extra={"sid": item["sessionId"]})
            try:
                await _settle(item, {"state": "failed"})
            except Exception:
                pass  # a newer run owns it, or the row expired


def _redact_line(line: bytes, project: str | None) -> bytes:
    end = b"\n" if line.endswith(b"\n") else b""
    text = line[: len(line) - len(end)].decode("utf-8", errors="replace")
    try:
        body = json.dumps(redact_value(json.loads(text), project), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        body = redact(text, project)
    return body.encode("utf-8") + end


def _measure(file) -> tuple[int, str]:
    file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(1 << 20), b""):
        digest.update(chunk)
    size = file.tell()
    file.seek(0)
    return size, digest.hexdigest()


def _redact_copy(row: dict, manifest: dict, copy_id: str, project: str | None) -> dict:
    """Stream every transcript part through the redactor, one part at a time (parts are up to
    64 MiB, the snapshot up to 1 GiB): gunzip, redact each JSONL line, gzip, upload. A line the Mac
    split across parts (one longer than its chunk) is carried whole into the next part. Sync:
    call through run_in_thread."""
    client, bucket = pickup._s3(), pickup._bucket()
    base = f"{pickup.session_prefix(row['ownerId'], row['sessionId'])}{row['pickupEpoch']}/copies/{copy_id}/"
    parts, carry, last = [], b"", len(manifest["parts"]) - 1
    for n, part in enumerate(manifest["parts"]):
        source = client.get_object(Bucket=bucket, Key=pickup._part_key(row, manifest["series"], part))["Body"]
        with tempfile.TemporaryFile() as out:
            with gzip.GzipFile(fileobj=source, mode="rb") as lines, gzip.GzipFile(fileobj=out, mode="wb") as gz:
                for line in lines:
                    line, carry = carry + line, b""
                    if not line.endswith(b"\n") and n != last:
                        carry = line
                        continue
                    gz.write(_redact_line(line, project))
            size, digest = _measure(out)
            key = f"{base}t-{part['index']:06d}.gz"
            client.upload_fileobj(out, bucket, key)
        parts.append({"index": part["index"], "bytes": size, "sha256": digest, "key": key})
    sidecars = None
    if manifest.get("sidecars"):
        source = client.get_object(Bucket=bucket, Key=pickup._key(row, manifest["series"], "sidecars", sha=manifest["sidecars"]["sha256"]))["Body"]
        with tempfile.TemporaryFile() as out:
            with tarfile.open(fileobj=source, mode="r|gz") as tin, tarfile.open(fileobj=out, mode="w:gz") as tout:
                for member in tin:
                    if member.isdir():
                        tout.addfile(member)
                        continue
                    if not member.isfile():
                        continue
                    # ponytail: one sidecar file is read whole; stream it if a single file nears memory limits.
                    data = tin.extractfile(member).read()
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError:
                        continue  # binary entries are dropped, never shown unredacted
                    body = b"".join(_redact_line(line, project) for line in data.splitlines(keepends=True))
                    info = tarfile.TarInfo(member.name)
                    info.size, info.mode, info.mtime = len(body), member.mode, member.mtime
                    tout.addfile(info, io.BytesIO(body))
            size, digest = _measure(out)
            key = f"{base}sidecars.tgz"
            client.upload_fileobj(out, bucket, key)
        sidecars = {"bytes": size, "sha256": digest, "key": key}
    return {"parts": parts, "sidecars": sidecars}


async def redeem(code: str, verifier: str) -> CopyTicket | Preparing:
    """The CLI's poll. The code alone is useless: it needs the verifier whose hash the confirm
    page sent, so a code left in browser history redeems nothing."""
    gone = ApiProblem("LOCAL_SESSION_NOT_FOUND", "This continue code is no longer valid. Press Continue again.")
    item = await store._get(_copy_pk(code), "META")
    now = int(time.time())
    if (
        item is None
        or now >= int(item["expiresAt"])
        or not hmac.compare_digest(item["challenge"], challenge_of(verifier))
        or now >= max(int(item["createdAt"]), int(item.get("readyAt", 0))) + CODE_WINDOW_S
    ):
        raise gone
    row = await repo.get_session(item["ownerId"], item["sessionId"])
    if (
        row is None
        or (row.get("shareLink") or {}).get("tokenHash") != item["linkHash"]
        or row.get("pickupEpoch") != item["epoch"]
    ):
        raise gone  # the link was turned off (or the snapshot retired) since Continue was pressed
    if item["state"] == "failed":
        raise ApiProblem("LOCAL_SESSION_CONFLICT", "This copy could not be prepared. Press Continue again.")
    if item["state"] == "preparing":
        if now - int(item["startedAt"]) > RESTART_AFTER_S:
            try:
                await _settle(item, {"startedAt": now})
                _start({**item, "startedAt": now})
            except Exception:
                pass  # another poll restarted it first
        return Preparing()
    manifest = await store._get(row["pk"], "PICKUP") if item["member"] else None
    owner = await _name(row["humanUserId"], first=True)
    return await run_in_thread(_copy_ticket, row, item, manifest, owner)


def _copy_ticket(row: dict, item: dict, manifest: dict | None, owner: str) -> CopyTicket:
    if manifest is not None:  # a member: the snapshot as it is, Pick up's behaviour
        code = manifest.get("code")
        downloads = pickup.downloads(row, manifest)
        mode = CopyCodeMode(mode="bundle", remote=code["remote"], snapshot=code["snapshot"], session_id=row["sessionId"]) if code else CopyCodeMode(mode="none")
        meta = (manifest["conversationId"], manifest["claudeVersion"], manifest["cwdRelative"] if code else "")
    else:
        client, bucket = pickup._s3(), pickup._bucket()

        def url(key: str) -> str:
            return client.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=pickup.URL_SECONDS)

        side = item.get("sidecars")
        downloads = pickup.Downloads(
            transcript=[pickup.TranscriptDownload(index=p["index"], url=url(p["key"]), bytes=p["bytes"], sha256=p["sha256"]) for p in item["parts"]],
            code=None,
            sidecars=pickup.Download(url=url(side["key"]), bytes=side["bytes"], sha256=side["sha256"]) if side else None,
        )
        repo_info = item.get("repo") or {}
        mode = CopyCodeMode(mode="clone", remote=repo_info["remote"], commit=repo_info["head"]) if item["codeMode"] == "clone" else CopyCodeMode(mode="none")
        meta = (item["conversationId"], item["claudeVersion"], item.get("cwdRelative", ""))
    return CopyTicket(
        title=_title(row, item["member"]), from_=owner, conversation_id=meta[0], claude_version=meta[1],
        cwd_relative=meta[2], downloads=downloads, code=mode,
    )
```

`session_links.py` imports `session_pickup as pickup` at the top (Task 3). `session_pickup` imports `local_sessions` and `session_screen` but never `session_links`, so there is no cycle.

3c. `routers/messaging.py`:

```python
@router.post("/session-links/copies", response_model=session_links.CopyCode, status_code=201)
async def copy_session_link(body: session_links.CopyRequest, auth: AuthContext = Depends(get_human_session)):
    """Continue: the moment a copy is made (spec §3.5). Needs the taker's own workspace."""
    return await session_links.create_copy(auth, body.token, body.challenge)


@router.post("/public/session-copies/redeem", response_model=session_links.CopyTicket | session_links.Preparing)
async def redeem_session_copy(body: session_links.RedeemRequest):
    """The CLI's poll. No sign-in: the code plus the CLI's verifier is the credential."""
    return await session_links.redeem(body.code, body.verifier)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_copies.py tests/unit/services/test_session_pickup.py tests/unit/services/test_session_links_public.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_links.py apps/backend/core/services/session_pickup.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_session_copies.py
git commit -m "feat(sessions): Continue copies, redacted for outsiders, redeemed with the CLI's verifier"
```

---

### Task 5: repoInfo from the Mac

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/snapshot.rs` (after `capture_code`, `:413-558`; the commit body, `:776-808`)
- Test: same file, `mod tests`

**Interfaces:**
- Consumes: `session_pickup::{git, normalize_remote}`.
- Produces: `snapshot::repo_info(top: &Path) -> Value` (async; `{remote, branch, head, pushed, public, folder}`, `Null` with no `origin` or no commit); `snapshot::probe_command(remote: &str) -> tokio::process::Command`; commit body key `"repo"`.

- [ ] **Step 1: Write the failing test**

Add to `snapshot.rs`'s `mod tests` (beside the test that builds a repo with `remote add origin https://github.com/o/r.git`, `:966`; reuse its `g` helper pattern):

```rust
    #[tokio::test]
    async fn repo_info_reports_remote_branch_head_pushed_and_cached_public() {
        let dir = tempfile::tempdir().unwrap();
        let top = dir.path();
        let g = |args: &[&str]| assert!(std::process::Command::new("git").args(args).current_dir(top).status().unwrap().success());
        g(&["init", "-q", "-b", "main"]);
        g(&["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one"]);
        g(&["remote", "add", "origin", "https://github.com/o/r.git"]);
        remember_public("github.com/o/r", true); // no network in tests
        let info = repo_info(top).await;
        assert_eq!(info["remote"], "github.com/o/r");
        assert_eq!(info["branch"], "main");
        assert_eq!(info["pushed"], false);
        assert_eq!(info["public"], true);
        assert_eq!(info["folder"], top.file_name().unwrap().to_string_lossy().as_ref());
        g(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
        assert_eq!(repo_info(top).await["pushed"], true);
    }

    #[tokio::test]
    async fn repo_info_is_null_without_origin() {
        let dir = tempfile::tempdir().unwrap();
        std::process::Command::new("git").args(["init", "-q"]).current_dir(dir.path()).status().unwrap();
        assert!(repo_info(dir.path()).await.is_null());
    }

    #[test]
    fn the_public_probe_ignores_the_users_git_config() {
        // A global `url.<ssh>.insteadOf https://` would turn the anonymous probe into an SSH call
        // with the user's keys, and a private repo would read as public.
        let cmd = probe_command("github.com/o/r");
        let std = cmd.as_std();
        let env: std::collections::HashMap<_, _> = std.get_envs().collect();
        assert_eq!(env.get(std::ffi::OsStr::new("GIT_CONFIG_GLOBAL")).copied().flatten(), Some(std::ffi::OsStr::new("/dev/null")));
        assert_eq!(env.get(std::ffi::OsStr::new("GIT_CONFIG_NOSYSTEM")).copied().flatten(), Some(std::ffi::OsStr::new("1")));
        assert_eq!(env.get(std::ffi::OsStr::new("GIT_ASKPASS")).copied().flatten(), Some(std::ffi::OsStr::new("/usr/bin/false")));
        let args: Vec<_> = std.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args, ["-c", "credential.helper=", "ls-remote", "https://github.com/o/r.git", "HEAD"]);
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/desktop/src-tauri && cargo test --lib repo_info the_public_probe`
Expected: FAIL to compile with `cannot find function 'repo_info'`

- [ ] **Step 3: Write minimal implementation**

After `capture_code` in `snapshot.rs`:

```rust
/// Anonymous `ls-remote` answers, per normalized remote, for an hour (spec §3.3).
static PUBLIC: std::sync::LazyLock<std::sync::Mutex<HashMap<String, (Instant, bool)>>> = std::sync::LazyLock::new(Default::default);
const PUBLIC_FOR: Duration = Duration::from_secs(3600);

fn remember_public(remote: &str, public: bool) {
    PUBLIC.lock().unwrap_or_else(|e| e.into_inner()).insert(remote.to_string(), (Instant::now(), public));
}

fn known_public(remote: &str) -> Option<bool> {
    let cache = PUBLIC.lock().unwrap_or_else(|e| e.into_inner());
    cache.get(remote).filter(|(at, _)| at.elapsed() < PUBLIC_FOR).map(|(_, public)| *public)
}

/// The anonymous probe. Not `git_run`: that keeps the user's global config on purpose, and a
/// global `url.<ssh>.insteadOf https://` would make this an SSH call with their keys. No config,
/// no credential helper, no prompt: exit 0 means anyone can read the repository.
fn probe_command(remote: &str) -> tokio::process::Command {
    let mut command = tokio::process::Command::new("/usr/bin/git");
    command
        .args(["-c", "credential.helper=", "ls-remote", &format!("https://{remote}.git"), "HEAD"])
        .current_dir(std::env::temp_dir())
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_ASKPASS", "/usr/bin/false")
        .env("SSH_ASKPASS", "/usr/bin/false")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .kill_on_drop(true);
    command
}

async fn public(remote: &str) -> bool {
    if let Some(known) = known_public(remote) {
        return known;
    }
    let answer = tokio::time::timeout(Duration::from_secs(15), probe_command(remote).status())
        .await
        .is_ok_and(|status| status.is_ok_and(|s| s.success()));
    remember_public(remote, answer);
    answer
}

/// `{remote, branch, head, pushed, public, folder}` for the commit body, or null when this is not
/// a work tree with an `origin` and a commit. Sent on every commit, bundle or not (spec §3.3).
/// `folder` is the top-level folder's name: the server hides it from outsiders when `public` is false.
pub(crate) async fn repo_info(top: &Path) -> Value {
    let Some(remote) = git(top, &["remote", "get-url", "origin"], None).await.ok().and_then(|o| normalize_remote(&o)) else {
        return Value::Null;
    };
    let Ok(head) = git(top, &["rev-parse", "--verify", "HEAD"], None).await else {
        return Value::Null;
    };
    let pushed = git(top, &["for-each-ref", "--count=1", "--contains", &head, "refs/remotes/origin"], None)
        .await
        .is_ok_and(|out| !out.is_empty());
    let branch = git(top, &["symbolic-ref", "--short", "-q", "HEAD"], None).await.ok();
    let folder = top.file_name().map(|n| n.to_string_lossy().into_owned());
    json!({"remote": remote, "branch": branch, "head": head, "pushed": pushed, "public": public(&remote).await, "folder": folder})
}
```

`snapshot.rs` already imports `HashMap`, `Duration` and `Instant` (`:10-11`) and uses `json!`.

In the commit body (`:789`), before `let mut body = json!({`:

```rust
    let repo_info = match top {
        Some(t) => repo_info(t).await,
        None => Value::Null,
    };
```

and add `"repo": repo_info,` to the `json!` object after `"sidecars"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/desktop/src-tauri && cargo test --lib snapshot`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/snapshot.rs
git commit -m "feat(desktop): report repo, branch, head, pushed and public with each pick-up snapshot"
```

---

### Task 6: The Continue flow on the receiver's Mac (`session_continue.rs`)

**Files:**
- Modify: `apps/desktop/src-tauri/src/session_pickup.rs` (visibility; extract `downloads_ok`, `fetch_downloads`, `exclude_worktrees`)
- Modify: `apps/desktop/src-tauri/src/local_sessions/plugin.rs` (`install_now`), `src/local_sessions.rs:10` (`pub(crate) mod plugin;`)
- Modify: `apps/desktop/src-tauri/src/lib.rs:22` (`mod session_continue;`)
- Create: `apps/desktop/src-tauri/src/session_continue.rs`

**Interfaces:**
- Consumes: Task 4's ticket shape (`downloads` = pick-up `Downloads`; `code = {mode, remote?, commit?, snapshot?, sessionId?}`; redeem body `{code, verifier}`, error body `{code, message}`); `crate::LIGHTBULB_API_BASE` (`lib.rs:43`); `crate::KEYCHAIN_SERVICE` (`lib.rs:39`, the bundle id; `start_links.rs:39` already builds the app's state dir from it); `session_pickup::{allowed_url, argv, workdir, import_code, normalize_remote, slug, older, hex_sha, claude_version, prune, git, git_run}`; `crate::local_sessions::provider_program`.
- Claude Code flags: Pick up's `argv` (`session_pickup.rs:624-638`) already ships `--resume <file> --fork-session --session-id <uuid> --setting-sources user`, so the fork lands under the id the CLI chose and the marker can be named before Claude starts. No separate check.
- Produces:
  - `session_pickup::{Download, Downloads}` and their fields `pub(crate)`; `pub(crate) fn downloads_ok(d: &Downloads) -> bool`; `pub(crate) async fn fetch_downloads(d: &Downloads, conversation: &str, dir: &Path) -> Result<PathBuf, Failure>`; `pub(crate) fn exclude_worktrees(repo: &Path) -> Result<(), String>` (async)
  - `plugin::install_now(state_dir: &Path, home: &Path) -> Result<()>`
  - `session_continue::main(link: &str) -> i32` (used by Task 7)
  - `session_continue::{parse_link, Link, verifier_and_challenge, wait_for_code, parse_copy, CopyTicket, clone_of, write_marker}`

- [ ] **Step 1: Write the failing tests**

At the end of the new `session_continue.rs` (start the file with only `#[cfg(test)] mod tests {…}` and the `use` lines, so it compiles to a test failure):

```rust
#[cfg(test)]
mod tests {
    use super::*;

    const API: &str = "https://api-dev.thelightbulb.company";
    const TOKEN: &str = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";

    #[test]
    fn only_this_builds_site_and_a_well_formed_token() {
        let link = parse_link(&format!("https://dev.thelightbulb.company/s#{TOKEN}"), API).unwrap();
        assert_eq!(link.web, "https://dev.thelightbulb.company");
        assert_eq!(link.api, "https://api-dev.thelightbulb.company/api/v1");
        assert_eq!(link.token, TOKEN);
        assert_eq!(parse_link(&format!("https://thelightbulb.company/s#{TOKEN}"), "https://api.thelightbulb.company").unwrap().web, "https://thelightbulb.company");
        for bad in [
            format!("https://thelightbulb.company/s#{TOKEN}"),  // the other environment
            format!("https://evil.example/s#{TOKEN}"),
            format!("http://dev.thelightbulb.company/s#{TOKEN}"),
            format!("https://dev.thelightbulb.company/x#{TOKEN}"),
            "https://dev.thelightbulb.company/s#short".to_string(),
            format!("https://dev.thelightbulb.company/s#{TOKEN}%20"),
        ] {
            assert!(parse_link(&bad, API).is_none(), "{bad}");
        }
    }

    #[test]
    fn the_challenge_is_the_verifiers_sha256() {
        use base64::Engine;
        let (verifier, challenge) = verifier_and_challenge();
        assert_eq!(verifier.len(), 43);
        let want = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        assert_eq!(challenge, want);
    }

    #[test]
    fn the_loopback_takes_only_done_with_a_code() {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let client = std::thread::spawn(move || {
            for path in ["/favicon.ico", &format!("/done?code={TOKEN}")] {
                let mut s = std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();
                write!(s, "GET {path} HTTP/1.1\r\nHost: x\r\n\r\n").unwrap();
                let mut answer = String::new();
                let _ = s.read_to_string(&mut answer);
            }
        });
        assert_eq!(wait_for_code(&listener).as_deref(), Some(TOKEN));
        client.join().unwrap();
    }

    fn ticket(code: Value, bundle: Value) -> Value {
        let url = "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/session-pickup/o/s/e/copies/c/t-000000.gz";
        json!({"state": "ready", "title": "Fix login", "from": "Alice", "conversationId": "11111111-2222-4333-8444-555555555555",
            "claudeVersion": "2.1.283", "cwdRelative": "", "code": code,
            "downloads": {"transcript": [{"index": 0, "url": url, "bytes": 3, "sha256": "a".repeat(64)}], "code": bundle, "sidecars": null}})
    }

    #[test]
    fn a_copy_ticket_is_checked_before_any_download() {
        assert!(parse_copy(ticket(json!({"mode": "none"}), Value::Null)).is_ok());
        assert!(parse_copy(ticket(json!({"mode": "clone", "remote": "github.com/o/r", "commit": "a".repeat(40)}), Value::Null)).is_ok());
        assert!(parse_copy(ticket(json!({"mode": "clone", "remote": "github.com/o/r", "commit": "main"}), Value::Null)).is_err());
        assert!(parse_copy(ticket(json!({"mode": "none"}), json!({"url": "https://evil.example/x", "bytes": 1, "sha256": "a".repeat(64)}))).is_err());
        assert!(parse_copy(ticket(json!({"mode": "bundle", "remote": "github.com/o/r", "snapshot": "b".repeat(40),
            "sessionId": "11111111-2222-4333-8444-555555555555"}), Value::Null)).is_err()); // bundle mode needs the bundle
        let mut off_bucket = ticket(json!({"mode": "none"}), Value::Null);
        off_bucket["downloads"]["transcript"][0]["url"] = json!("https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/other/x");
        assert!(parse_copy(off_bucket).is_err());
    }

    #[tokio::test]
    async fn the_current_folder_is_used_when_it_is_a_clone_of_the_remote() {
        let dir = tempfile::tempdir().unwrap();
        let g = |args: &[&str]| assert!(std::process::Command::new("git").args(args).current_dir(dir.path()).status().unwrap().success());
        g(&["init", "-q"]);
        g(&["remote", "add", "origin", "git@github.com:o/r.git"]);
        let here = std::fs::canonicalize(dir.path()).unwrap();
        assert_eq!(clone_of(&here, "github.com/o/r").await.unwrap().0, here);
    }

    #[test]
    fn the_marker_names_the_fork() {
        let home = tempfile::tempdir().unwrap();
        write_marker(home.path(), "fork-1", "Alice\u{1b}[31m", Some("Fix login")).unwrap();
        let text = std::fs::read_to_string(home.path().join(".lightbulb/continued/fork-1.json")).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap(), json!({"from": "Alice[31m", "title": "Fix login"}));
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/desktop/src-tauri && cargo test --lib session_continue`
Expected: FAIL to compile with `cannot find function 'parse_link'`

- [ ] **Step 3: Refactor `session_pickup.rs` for reuse (no behaviour change)**

- `Download`/`Downloads`: `pub(crate) struct`, every field `pub(crate)`.
- Make these `pub(crate)`: `slug`, `older`, `hex_sha`, `argv`, `workdir`, `claude_version`, `prune` and the `Failure` type alias.
- Extract from `parse_ticket` the download checks:

```rust
/// Every URL in the bucket's pick-up path, digests well formed, transcript non-empty and in
/// order, each part and the total under their caps. Shared by pick-up tickets and copy tickets.
pub(crate) fn downloads_ok(d: &Downloads) -> bool {
    let all: Vec<&Download> = d.transcript.iter().chain(&d.code).chain(&d.sidecars).collect();
    let total = all.iter().fold(0u64, |sum, p| sum.saturating_add(p.bytes));
    all.iter().all(|p| allowed_url(&p.url) && p.sha256.len() == 64 && p.sha256.bytes().all(|b| b.is_ascii_hexdigit()))
        && !d.transcript.is_empty()
        && d.transcript.iter().all(|p| p.bytes <= PART_MAX)
        && d.transcript.iter().enumerate().all(|(i, p)| p.index == i as u64)
        && d.code.as_ref().is_none_or(|c| c.bytes <= CODE_MAX)
        && d.sidecars.as_ref().is_none_or(|s| s.bytes <= SIDECARS_MAX)
        && total <= TOTAL_MAX
}
```

`parse_ticket` keeps its two distinct errors. If any URL fails `allowed_url`, it returns `URL_REFUSED` as today. Otherwise `!downloads_ok(d) || d.code.is_some() != … || <manifest code check>` returns `INVALID_TICKET`.
- Rename `fetch_parts`'s body to `pub(crate) async fn fetch_downloads(d: &Downloads, conversation: &str, dir: &Path)`. Replace `ticket.downloads` with `d` and `ticket.manifest.conversation_id` with `conversation`. `fetch_parts` becomes `fetch_downloads(&ticket.downloads, &ticket.manifest.conversation_id, dir).await`.
- Extract the `check-ignore`/`info/exclude` block of `import_code` (`:485-506`) into `pub(crate) async fn exclude_worktrees(repo: &Path) -> Result<(), String>`. `import_code` calls it with `.map_err(|m| failed(&m))?`.

Run `cargo test --lib session_pickup`. Expected: PASS, with the same tests as before.

- [ ] **Step 4: Add `plugin::install_now`**

In `local_sessions.rs:10` change `mod plugin;` to `pub(crate) mod plugin;`. In `plugin.rs`:

```rust
/// For `lightbulb continue`: write the mod and install it now, in the foreground (the CLI exits
/// into Claude right after). Same files and the same two commands as `install`.
pub(crate) fn install_now(state_dir: &Path, home: &Path) -> Result<()> {
    let root = write_files(state_dir)?;
    run_all(home.to_owned(), commands(&root, true));
    Ok(())
}
```

- [ ] **Step 5: Write the implementation**

`session_continue.rs` (above the tests):

```rust
//! `lightbulb continue '<link>'` (spec 2026-10-05-public-session-links §3.6): take a copy of a
//! shared Claude Code session and resume it here, in this terminal, as the receiver's own
//! session. Their Mac, their Claude, their plan; the original session and its Mac are never
//! contacted. Everything the server sends is untrusted: it is checked with pick-up's own checks.
use crate::session_pickup::{self as pickup, git, git_run, normalize_remote, Downloads};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

const ID: &str = "lightbulb-sessions@lightbulb";

pub(crate) struct Link {
    pub web: String,
    pub api: String,
    pub token: String,
}

fn url_safe(s: &str) -> bool {
    (16..=128).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// The link must be on this build's own site (`api[-dev].X` ↔ `[dev.]X`): its token and the
/// verifier are sent nowhere else.
pub(crate) fn parse_link(raw: &str, api_base: &str) -> Option<Link> {
    let url = url::Url::parse(raw.trim()).ok()?;
    let api_host = url::Url::parse(api_base).ok()?.host_str()?.to_string();
    let web_host = match api_host.strip_prefix("api-dev.") {
        Some(rest) => format!("dev.{rest}"),
        None => api_host.strip_prefix("api.")?.to_string(),
    };
    let token = url.fragment()?;
    (url.scheme() == "https" && url.port().is_none() && url.host_str() == Some(web_host.as_str()) && url.path() == "/s" && url_safe(token))
        .then(|| Link {
            web: format!("https://{web_host}"),
            api: format!("{}/api/v1", api_base.trim_end_matches('/')),
            token: token.to_string(),
        })
}

pub(crate) fn verifier_and_challenge() -> (String, String) {
    use base64::Engine;
    let b64 = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let verifier = b64.encode(rand::random::<[u8; 32]>());
    let challenge = b64.encode(Sha256::digest(verifier.as_bytes()));
    (verifier, challenge)
}

/// The confirm page's redirect: `GET /done?code=…`. Anything else (a favicon) gets a 404 and is
/// waited past. Blocks until it arrives; Ctrl-C ends the command.
// ponytail: no deadline; add one if people leave the command waiting.
pub(crate) fn wait_for_code(listener: &std::net::TcpListener) -> Option<String> {
    loop {
        let (mut stream, _) = listener.accept().ok()?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
        let mut line = String::new();
        let _ = BufReader::new(&stream).read_line(&mut line);
        let code = line
            .strip_prefix("GET /done?code=")
            .and_then(|rest| rest.split(' ').next())
            .filter(|c| url_safe(c))
            .map(str::to_string);
        let (status, body) = match code {
            Some(_) => ("200 OK", "Continuing in your terminal. You can close this tab."),
            None => ("404 Not Found", "Not found"),
        };
        let _ = write!(stream, "HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        if code.is_some() {
            return code;
        }
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CopyCode {
    mode: String,
    remote: Option<String>,
    commit: Option<String>,
    snapshot: Option<String>,
    session_id: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CopyTicket {
    title: Option<String>,
    from: String,
    conversation_id: String,
    claude_version: String,
    #[serde(default)]
    cwd_relative: String,
    downloads: Downloads,
    code: CopyCode,
}

pub(crate) fn parse_copy(value: Value) -> Result<CopyTicket, String> {
    let bad = || "Lightbulb sent a copy this app will not use.".to_string();
    let t: CopyTicket = serde_json::from_value(value).map_err(|_| bad())?;
    let remote_ok = t.code.remote.as_deref().is_some_and(|r| normalize_remote(&format!("https://{r}")).as_deref() == Some(r));
    let mode_ok = match t.code.mode.as_str() {
        "none" => t.downloads.code.is_none(),
        "clone" => remote_ok && t.code.commit.as_deref().is_some_and(pickup::hex_sha) && t.downloads.code.is_none(),
        "bundle" => {
            remote_ok
                && t.code.snapshot.as_deref().is_some_and(pickup::hex_sha)
                && t.code.session_id.as_deref().is_some_and(|s| uuid::Uuid::parse_str(s).is_ok())
                && t.downloads.code.is_some()
        }
        _ => false,
    };
    (uuid::Uuid::parse_str(&t.conversation_id).is_ok() && pickup::downloads_ok(&t.downloads) && mode_ok)
        .then_some(t)
        .ok_or_else(bad)
}

/// A clone of `remote` to work in: the current folder's repository when its origin matches,
/// else a fresh `git clone` into `./<name>`. The bool says whether it was cloned just now.
pub(crate) async fn clone_of(here: &Path, remote: &str) -> Result<(PathBuf, bool), String> {
    if let Ok(top) = git(here, &["rev-parse", "--show-toplevel"], None).await {
        let top = PathBuf::from(top);
        if git(&top, &["remote", "get-url", "origin"], None).await.ok().and_then(|o| normalize_remote(&o)).as_deref() == Some(remote) {
            return Ok((top, false));
        }
    }
    let name = remote.rsplit('/').next().filter(|n| !n.starts_with(['.', '-']) && n.bytes().all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))).ok_or("the repository name is not usable here")?;
    let dest = here.join(name);
    if dest.exists() {
        return Err(format!("./{name} already exists"));
    }
    match git_run(here, &["clone", "-q", "--", &format!("https://{remote}.git"), name], None, Duration::from_secs(900)).await? {
        (true, _) => Ok((dest, true)),
        _ => Err("git clone failed".into()),
    }
}

/// The folder Claude starts in. A failure prints why and falls back to the conversation only.
async fn get_code(t: &CopyTicket, dir: &Path, here: &Path) -> PathBuf {
    let title = t.title.as_deref().unwrap_or("");
    let short: String = uuid::Uuid::new_v4().simple().to_string()[..6].to_string();
    let result: Result<PathBuf, String> = async {
        let remote = t.code.remote.as_deref().unwrap_or_default();
        match t.code.mode.as_str() {
            "clone" => {
                let commit = t.code.commit.as_deref().unwrap_or_default();
                let branch = format!("continue/{}-{short}", pickup::slug(title));
                let (repo, fresh) = clone_of(here, remote).await?;
                if fresh {
                    git(&repo, &["checkout", "-q", "-b", &branch, commit], None).await?;
                    return Ok(pickup::workdir(&repo, &t.cwd_relative));
                }
                let _ = git_run(&repo, &["fetch", "-q", "origin", commit], None, Duration::from_secs(300)).await;
                pickup::exclude_worktrees(&repo).await?;
                let tree = repo.join(".claude/worktrees").join(format!("continue-{}-{short}", pickup::slug(title)));
                git(&repo, &["worktree", "add", "-q", "-b", &branch, &tree.to_string_lossy(), commit], None).await?;
                Ok(pickup::workdir(&tree, &t.cwd_relative))
            }
            "bundle" => {
                let (repo, _) = clone_of(here, remote).await?;
                let sid = t.code.session_id.as_deref().unwrap_or_default();
                let snapshot = t.code.snapshot.as_deref().unwrap_or_default();
                let (_, tree) = pickup::import_code(&repo, &dir.join("code.bundle"), sid, &short, title, snapshot)
                    .await
                    .map_err(|e| e["message"].as_str().unwrap_or("import failed").to_string())?;
                Ok(pickup::workdir(&tree, &t.cwd_relative))
            }
            _ => Ok(here.to_path_buf()),
        }
    }
    .await;
    result.unwrap_or_else(|reason| {
        eprintln!("Couldn't get the code ({reason}). Continuing with the conversation only.");
        here.to_path_buf()
    })
}

fn ensure_mod(claude: &Path, home: &Path) {
    let listed = std::process::Command::new(claude).args(["plugin", "list"]).stdin(std::process::Stdio::null()).output();
    if listed.is_ok_and(|o| String::from_utf8_lossy(&o.stdout).contains(ID)) {
        return;
    }
    let state = home.join("Library/Application Support").join(crate::KEYCHAIN_SERVICE).join("local-sessions");
    if let Err(e) = crate::local_sessions::plugin::install_now(&state, home) {
        eprintln!("lightbulb: the Lightbulb mod was not installed ({e}).");
    }
}

/// Read by the mod's band (Task 8). Names come from the server: no control characters.
fn write_marker(home: &Path, session: &str, from: &str, title: Option<&str>) -> std::io::Result<()> {
    let clean = |s: &str| s.chars().filter(|c| !c.is_control()).take(80).collect::<String>();
    let dir = home.join(".lightbulb/continued");
    std::fs::create_dir_all(&dir)?;
    std::fs::write(dir.join(format!("{session}.json")), json!({"from": clean(from), "title": title.map(clean)}).to_string())
}

async fn redeem(client: &reqwest::Client, api: &str, code: &str, verifier: &str) -> Result<Value, String> {
    for _ in 0..450 {
        let res = client
            .post(format!("{api}/public/session-copies/redeem"))
            .json(&json!({"code": code, "verifier": verifier}))
            .send()
            .await
            .map_err(|_| "Lightbulb could not be reached.".to_string())?;
        let ok = res.status().is_success();
        let body: Value = res.json().await.unwrap_or(Value::Null);
        if !ok {
            return Err(body["message"].as_str().unwrap_or("Lightbulb refused the copy.").to_string());
        }
        if body["state"] == "ready" {
            return Ok(body);
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    Err("The copy took too long to prepare. Press Continue again.".into())
}

async fn run(raw: &str) -> Result<std::convert::Infallible, String> {
    let link = parse_link(raw, crate::LIGHTBULB_API_BASE).ok_or("That is not a Lightbulb session link for this app.")?;
    let home = std::env::var_os("HOME").map(PathBuf::from).ok_or("HOME is not set.")?;
    let claude = crate::local_sessions::provider_program(&home, "claude").ok_or("Claude Code is not installed on this Mac.")?;
    let here = std::env::current_dir().map_err(|_| "This folder no longer exists.")?;
    let (verifier, challenge) = verifier_and_challenge();
    let listener = std::net::TcpListener::bind("127.0.0.1:0").map_err(|_| "Could not listen on this Mac.")?;
    let port = listener.local_addr().map_err(|_| "Could not listen on this Mac.")?.port();
    let confirm = format!("{}/s?port={port}&challenge={challenge}#{}", link.web, link.token);
    eprintln!("Confirm in your browser. If it did not open, visit:\n  {confirm}");
    let _ = std::process::Command::new("/usr/bin/open").arg(&confirm).status();
    let code = tokio::task::spawn_blocking(move || wait_for_code(&listener)).await.ok().flatten().ok_or("No confirmation arrived.")?;
    let client = reqwest::Client::builder().redirect(reqwest::redirect::Policy::none()).timeout(Duration::from_secs(60)).build().map_err(|_| "Could not start a request.")?;
    eprintln!("Preparing the copy…");
    let ticket = parse_copy(redeem(&client, &link.api, &code, &verifier).await?)?;
    let installed = pickup::claude_version(&claude).await.unwrap_or_default();
    if pickup::older(&installed, &ticket.claude_version) {
        return Err(format!("Update Claude Code to continue this session (it needs {}).", ticket.claude_version));
    }
    let root = home.join(".lightbulb/continue");
    std::fs::create_dir_all(&root).map_err(|_| "~/.lightbulb is not writable.")?;
    pickup::prune(&root);
    let dir = root.join(uuid::Uuid::new_v4().simple().to_string());
    std::fs::create_dir_all(&dir).map_err(|_| "~/.lightbulb is not writable.")?;
    let transcript = pickup::fetch_downloads(&ticket.downloads, &ticket.conversation_id, &dir)
        .await
        .map_err(|e| e["message"].as_str().unwrap_or("The download failed.").to_string())?;
    let cwd = get_code(&ticket, &dir, &here).await;
    ensure_mod(&claude, &home);
    let session = uuid::Uuid::new_v4().to_string();
    let _ = write_marker(&home, &session, &ticket.from, ticket.title.as_deref());
    let argv = pickup::argv(&claude, &transcript, &session);
    let err = std::process::Command::new(&argv[0]).args(&argv[1..]).current_dir(cwd).exec();
    Err(format!("could not run claude: {err}"))
}

/// `lightbulb continue '<link>'`: the process exit code (on success this process becomes Claude).
pub fn main(link: &str) -> i32 {
    let Ok(rt) = tokio::runtime::Builder::new_current_thread().enable_all().build() else {
        return 1;
    };
    match rt.block_on(run(link)) {
        Ok(never) => match never {},
        Err(message) => {
            eprintln!("lightbulb: {message}");
            1
        }
    }
}
```

In `lib.rs`, next to `mod session_pickup;`, add `mod session_continue;`. `cli` is in the same crate, so it reaches `crate::session_continue::main` even though the module is private.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd apps/desktop/src-tauri && cargo test --lib session_continue session_pickup plugin`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/desktop/src-tauri/src/session_continue.rs apps/desktop/src-tauri/src/session_pickup.rs apps/desktop/src-tauri/src/local_sessions/plugin.rs apps/desktop/src-tauri/src/local_sessions.rs apps/desktop/src-tauri/src/lib.rs
git commit -m "feat(desktop): continue a shared session from its link, reusing pick-up's checks"
```

---

### Task 7: `lightbulb continue` in the desktop CLI

No open PR touches `cli.rs` (checked 2026-10-05). Keep the diff to the lines below.

**Files:**
- Modify: `apps/desktop/src-tauri/src/cli.rs:271-272` (USAGE), `:526-535` (`main`), `mod tests`

**Interfaces:**
- Consumes: Task 6 `crate::session_continue::main(link: &str) -> i32`.
- Produces: `cli::continue_link(args: &[String]) -> Option<Result<&str, ()>>`.

- [ ] **Step 1: Write the failing test**

In `cli.rs`'s `mod tests`:

```rust
    #[test]
    fn continue_takes_exactly_one_link() {
        assert_eq!(continue_link(&words(&["continue", "https://x/s#t"])), Some(Ok("https://x/s#t")));
        assert_eq!(continue_link(&words(&["continue"])), Some(Err(())));
        assert_eq!(continue_link(&words(&["continue", "a", "b"])), Some(Err(())));
        assert_eq!(continue_link(&words(&["claude", "continue"])), None);
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/desktop/src-tauri && cargo test --lib continue_takes_exactly_one_link`
Expected: FAIL to compile with `cannot find function 'continue_link'`

- [ ] **Step 3: Write minimal implementation**

```rust
const USAGE: &str = "Usage: lightbulb claude|codex [any claude or codex arguments]\n\
       lightbulb continue '<link>'\n\
Starts a session in the Lightbulb app on this Mac, with its live terminal there too, or continues\n\
a shared session from its link in your own Claude Code.";

/// `lightbulb continue '<link>'`: Some when the first word is `continue`.
pub fn continue_link(args: &[String]) -> Option<Result<&str, ()>> {
    match args {
        [first, link] if first == "continue" => Some(Ok(link.as_str())),
        [first, ..] if first == "continue" => Some(Err(())),
        _ => None,
    }
}
```

At the very top of `pub fn main(args: &[String]) -> i32`, **before** `block_signals()`, so Ctrl-C works while it waits for the browser:

```rust
    if let Some(link) = continue_link(args) {
        return match link {
            Ok(link) => crate::session_continue::main(link),
            Err(()) => {
                eprintln!("Usage: lightbulb continue '<link>'");
                2
            }
        };
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/desktop/src-tauri && cargo test --lib cli`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/cli.rs
git commit -m "feat(desktop): lightbulb continue '<link>'"
```

---

### Task 8: The "Continued from" band in the mod

**Files:**
- Modify: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/state.ts`, `hooks/register.tsx`, `types/index.d.ts`
- Test: `apps/desktop/src-tauri/mods/lightbulb-sessions/hooks/band.test.ts`

**Interfaces:**
- Consumes: Task 6's marker `~/.lightbulb/continued/<session id>.json` = `{from: string, title: string | null}`.
- Produces: `state.ts` `Continued`, `parseContinued(text: string): Continued | null`, `continuedLine(c: Continued): string`; `PluginState['lightbulb-sessions'].continued`.

- [ ] **Step 1: Write the failing test**

Append to `band.test.ts`:

```ts
import { continuedLine, parseContinued } from './state'

test('a continued marker becomes one line; junk becomes nothing', () => {
  const c = parseContinued('{"from":"Prasiddha","title":"Fix login"}')!
  expect(continuedLine(c)).toBe("Continued from Prasiddha's session · Share yours (thelightbulb.company)")
  expect(parseContinued('{"from":""}')).toBeNull()
  expect(parseContinued('not json')).toBeNull()
  expect(parseContinued('{"from":"A\\u001b[31mB"}')!.from).toBe('A[31mB')
})

test('an unshared continued session shows the Continued-from band', async ($, on) => {
  on('session.id', () => ({ value: 'fork-1' }))
  on('env.get', ($, e) => ({ value: e.name === 'HOME' ? '/Users/r' : undefined }))
  on('fs.read', ($, e) =>
    e.path === '/Users/r/.lightbulb/continued/fork-1.json'
      ? { value: '{"from":"Prasiddha","title":"Fix login"}' }
      : { deny: `no file ${e.path}` }) // no socket file: the app says nothing is shared
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => $.ui.resolve(e).Text({ children: ENGINE }))
  const clock = mock.clock(on)
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /Continued from Prasiddha's session · Share yours/ })).toBeDefined()
  await ui.unmount()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions`
Expected: FAIL with `continuedLine is not exported` / `parseContinued is not exported`

- [ ] **Step 3: Write minimal implementation**

`types/index.d.ts`: add `export type Continued = { from: string; title: string | null }`, and add `continued: Continued | null` to the `'lightbulb-sessions'` entry of `PluginState`.

`state.ts`:

```ts
import type { Continued } from '../types'

// The marker `lightbulb continue` writes before it starts Claude. Its text came from a server.
export function parseContinued(text: string): Continued | null {
  try {
    const v = JSON.parse(text) as { from?: unknown; title?: unknown }
    const clean = (s: unknown) => (typeof s === 'string' ? s.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 80) : '')
    const from = clean(v.from)
    return from ? { from, title: clean(v.title) || null } : null
  } catch {
    return null
  }
}

export const continuedLine = (c: Continued) => `Continued from ${c.from}'s session · Share yours (thelightbulb.company)`
```

`register.tsx`:
- Import `continuedLine, parseContinued` from `./state` and `Continued` from `../types`.
- Add `const continued = atom({ plugin: 'lightbulb-sessions', key: 'continued' } as const, null as Continued | null)`.
- Add a function in this module, because the engine follows `$` only into functions the module itself declares:

```ts
async function continuedMarker($: EngineInterface): Promise<Continued | null> {
  try {
    const home = await $.env.get('HOME')
    if (!home) return null
    const text = await $.fs.read(`${home}/.lightbulb/continued/${await $.session.id()}.json`)
    return typeof text === 'string' ? parseContinued(text) : null
  } catch {
    return null // not a continued session
  }
}
```

- In the `session.start` hook, before `$.clock.after(0, refresh)`: `$.clock.after(0, async () => { const c = await continuedMarker($); if (c) await update($, continued, () => c) })`. Keep it off the start path, as the refresh is.
- In the `AbovePrompt` hook, replace `if (!s) return next(e)` with:

```tsx
    if (!s) {
      // Not shared itself: a continued session says where it came from (spec §3.6).
      const c = await read($, continued)
      if (!c) return next(e)
      const { Text } = $.ui.resolve(e)
      return <Text dimColor>{continuedLine(c)}</Text>
    }
```

- [ ] **Step 4: Run test and validation**

Run: `claude plugin test apps/desktop/src-tauri/mods/lightbulb-sessions && claude plugin validate apps/desktop/src-tauri/mods/lightbulb-sessions`
Expected: PASS. `validate` lists `HOME` among the env names read and reports nothing refused.
Run: `cd apps/desktop/src-tauri && cargo test --lib plugin_files_list_covers_every_non_test_mod_file`
Expected: PASS (no new mod file).

- [ ] **Step 5: Check `$.fs.read` under `~` in a real session (spec §3.6 open point)**

Run: `mkdir -p ~/.lightbulb/continued && SID=$(uuidgen | tr A-Z a-z) && echo '{"from":"Test"}' > ~/.lightbulb/continued/$SID.json && claude --plugin-dir apps/desktop/src-tauri/mods/lightbulb-sessions --session-id $SID`
Expected: the prompt shows `Continued from Test's session · Share yours (thelightbulb.company)`. Exit and delete the file. If the band does not appear and `claude --debug` logs an `fs.read` refusal, move the marker to `${$.plugin.root}/continued/<id>.json`. In Task 6's `write_marker`, write to `<state dir>/claude-mods/lightbulb-sessions/continued/` instead, and update both tests.

- [ ] **Step 6: Commit**

```bash
git add apps/desktop/src-tauri/mods/lightbulb-sessions
git commit -m "feat(mod): Continued from <name>'s session band"
```

---

### Task 9: Shared client and the owner's Share control

**Files:**
- Modify: `packages/shared/src/messaging/types.ts:513-534`, `client.ts:94-122, 186-199`, `sessionControls.ts:131-182, 333-338`, `src/index.ts:227-241`
- Create: `apps/frontend/src/components/messaging/SessionShareLink.tsx`, `__tests__/SessionShareLink.test.tsx`
- Delete: `apps/frontend/src/components/messaging/SessionShareLinks.tsx`, `__tests__/SessionShareLinks.test.tsx`
- Modify: `apps/frontend/src/components/messaging/SessionDetails.tsx:21-27, 83-90, 128-176`, `__tests__/SessionDetails.test.tsx`
- Test: `packages/shared/src/messaging/sessionControls.test.ts`

**Interfaces:**
- Consumes: Task 2 routes; Task 4 `POST /session-links/copies`.
- Produces (shared):
  - `SessionLinkAudience = "workspace" | "anyone"`; `SessionLink { url: string | null; audience }`
  - `SessionLinkPage`, `SessionLinkCode` (Task 3's `LinkPage` in camelCase); `SessionCopyCode { code: string }`
  - client: `getLocalSessionLink(sessionId)`, `putLocalSessionLink(sessionId, audience)`, `deleteLocalSessionLink(sessionId)`, `createSessionCopy(token, challenge)`
  - audit actions `share_link_audience`, `session_copied`
  - `LocalSessionPickup.privateRepo?: boolean` (Task 3's `PickupStatus.private_repo`)
  - Removed: `LocalSessionShareLink`, `SharedLocalSession`, `create/list/revokeLocalSessionShareLink`, `getSharedLocalSession`, `listSharedLocalSessionEvents`, `setLocalSessionPickup`, `useSessionPickupPermission`.

- [ ] **Step 1: Write the failing tests**

`packages/shared/src/messaging/sessionControls.test.ts`, add:

```ts
it("names who took a copy, from the entry's own detail", () => {
  const entry = { at: "2026-10-05T10:00:00Z", action: "session_copied", actorUserId: "user_out", detail: "Sam Lee" } as const;
  expect(auditLine(entry, () => "A teammate")).toBe("Sam Lee took a copy to continue in their Claude Code");
  expect(auditLine({ ...entry, action: "share_link_audience", actorUserId: "alice", detail: "anyone" }, () => "You"))
    .toBe("You changed who the link is for");
});
```

`apps/frontend/src/components/messaging/__tests__/SessionShareLink.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionShareLink } from "../SessionShareLink";

const session = { sessionId: "s1", provider: "claude" } as LocalSession;
const url = "https://dev.thelightbulb.company/s#" + "t".repeat(43);

it("Share makes the one link and copies it; the audience switch warns; Turn off removes it", async () => {
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
  const client = {
    getLocalSessionLink: vi.fn(async () => { throw new Error("404"); }),
    putLocalSessionLink: vi.fn(async (_: string, audience: "workspace" | "anyone") => ({ url, audience })),
    deleteLocalSessionLink: vi.fn(async () => {}),
  };
  render(<SessionShareLink session={session} client={client} />);
  await userEvent.click(await screen.findByRole("button", { name: "Share" }));
  expect(client.putLocalSessionLink).toHaveBeenCalledWith("s1", "workspace");
  expect(await screen.findByText("Link copied.")).toBeInTheDocument();
  expect(navigator.clipboard.writeText).toHaveBeenCalledWith(url);
  await userEvent.selectOptions(screen.getByLabelText("Who can open it"), "anyone");
  expect(client.putLocalSessionLink).toHaveBeenLastCalledWith("s1", "anyone");
  expect(await screen.findByRole("note")).toHaveTextContent("Anyone with the link can read this conversation");
  await userEvent.click(screen.getByRole("button", { name: "Turn off link" }));
  expect(client.deleteLocalSessionLink).toHaveBeenCalledWith("s1");
  expect(await screen.findByRole("button", { name: "Share" })).toBeInTheDocument();
});

it("warns that a private repository's code is in the conversation", async () => {
  const client = { getLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn() };
  render(<SessionShareLink session={{ ...session, pickup: { allowed: true, privateRepo: true } }} client={client} />);
  expect(await screen.findByText("This conversation includes code Claude read from your private repository.")).toBeInTheDocument();
});

it("shows an existing link's audience without creating one", async () => {
  const client = { getLocalSessionLink: vi.fn(async () => ({ url, audience: "anyone" as const })), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn() };
  render(<SessionShareLink session={session} client={client} />);
  expect(await screen.findByLabelText("Who can open it")).toHaveValue("anyone");
  expect(client.putLocalSessionLink).not.toHaveBeenCalled();
});

it("draws nothing without the link routes", () => {
  const { container } = render(<SessionShareLink session={session} client={{}} />);
  expect(container).toBeEmptyDOMElement();
});
```

In `SessionDetails.test.tsx`:
- In `client()`, replace `setLocalSessionPickup` and `createLocalSessionShareLink` with `getLocalSessionLink: vi.fn(async () => { throw new Error("404"); }), putLocalSessionLink: vi.fn(), deleteLocalSessionLink: vi.fn(), listLocalSessionAudit: vi.fn(async () => ({ entries: [] }))`.
- Change the first test's switch list to `["Shared with the workspace", "Let teammates control this session", "Show token usage to teammates"]`, and its button assertion to `await screen.findByRole("button", { name: "Share" })` (make the test `async`).
- Delete the pick-up clicks from "writes each switch…", and delete the tests "will not allow pick-up on a session that is not shared…", "follows the server when it answers other than asked" and "warns what pick-up uploads…".
- Add:

```tsx
it("lists who took a copy on Access", async () => {
  const c = { ...client(), listLocalSessionAudit: vi.fn(async () => ({ entries: [
    { at: "2026-10-05T10:00:00Z", action: "session_copied" as const, actorUserId: "user_out", detail: "Sam Lee" },
  ] })) };
  render(<Details c={c} />);
  expect(await screen.findByText(/Sam Lee/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd packages/shared && pnpm vitest run src/messaging/sessionControls.test.ts` and `cd apps/frontend && pnpm vitest run src/components/messaging/__tests__/SessionShareLink.test.tsx src/components/messaging/__tests__/SessionDetails.test.tsx`
Expected: FAIL (`session_copied` renders `undefined`; `Cannot find module '../SessionShareLink'`)

- [ ] **Step 3: Write minimal implementation**

`types.ts`: in `LocalSessionAuditEntry.action` add `| "share_link_audience" | "session_copied"`. In `LocalSessionPickup` add `/** The repository is private: its folder name is hidden from outsiders. */ privateRepo?: boolean;`. Replace `LocalSessionShareLink` and `SharedLocalSession` with:

```ts
export type SessionLinkAudience = "workspace" | "anyone";
/** The session's one link. `url` is null only when it can no longer be shown (turn it off and on). */
export interface SessionLink { url: string | null; audience: SessionLinkAudience }
export interface SessionLinkCode { visibility: "public" | "private" | "none" | "workspace"; remote?: string; branch?: string; commit?: string }
export interface SessionLinkPage {
  title: string | null; ownerFirstName: string; provider: "claude" | "codex"; state: "live" | "ended" | "offline";
  code: SessionLinkCode; continuable: boolean; me: { signedIn: boolean; member: boolean }; sessionId?: string;
}
export interface SessionCopyCode { code: string }
```

`client.ts`: in the interface and the factory, remove the share-link, shared-session and `setLocalSessionPickup` members. Add:

```ts
  getLocalSessionLink(sessionId: string): Promise<SessionLink>;
  putLocalSessionLink(sessionId: string, audience: SessionLinkAudience): Promise<SessionLink>;
  deleteLocalSessionLink(sessionId: string): Promise<void>;
  /** Continue: the moment a copy is made. The challenge is the CLI's; the token rides in the body. */
  createSessionCopy(token: string, challenge: string): Promise<SessionCopyCode>;
```
```ts
    getLocalSessionLink: (sessionId) => api.get(`${sessionPath(sessionId)}/link`) as Promise<SessionLink>,
    putLocalSessionLink: (sessionId, audience) => api.put(`${sessionPath(sessionId)}/link`, { audience }) as Promise<SessionLink>,
    deleteLocalSessionLink: async (sessionId) => { await api.del(`${sessionPath(sessionId)}/link`); },
    createSessionCopy: (token, challenge) => api.post("/session-links/copies", { token, challenge }) as Promise<SessionCopyCode>,
```

`sessionControls.ts`:
- `ACTION_LINE` += `share_link_audience: "changed who the link is for", session_copied: "took a copy to continue in their Claude Code"`.
- In `auditLine`, after the `pickup_expired` line: `if (entry.action === "session_copied") return `${entry.detail || (entry.actorUserId ? nameOf(entry.actorUserId) : "Someone")} ${ACTION_LINE.session_copied}`;`. The taker is often outside the workspace, so `detail` carries their name.
- Delete `useSessionPickupPermission`.
- `index.ts`: drop `useSessionPickupPermission`, `LocalSessionShareLink`, `SharedLocalSession`; export the new types.

`SessionShareLink.tsx`:

```tsx
"use client";

import { useEffect, useId, useState } from "react";
import { Link2 } from "lucide-react";
import type { createMessagingClient, LocalSession, SessionLink, SessionLinkAudience } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { copyText } from "./messageLinks";

type Client = Partial<Pick<ReturnType<typeof createMessagingClient>, "getLocalSessionLink" | "putLocalSessionLink" | "deleteLocalSessionLink">>;

/**
 * The session's one link (spec 2026-10-05-public-session-links §2.1): Share makes it and copies
 * it; the audience is the only setting; Turn off ends it everywhere. On a Claude session the link
 * is also what lets people pick it up or continue it.
 */
export function SessionShareLink({ session, client }: { session: LocalSession; client: Client }) {
  const [link, setLink] = useState<SessionLink | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const audienceId = useId();
  useEffect(() => {
    let alive = true;
    client.getLocalSessionLink?.(session.sessionId).then(l => { if (alive) setLink(l); }, () => {});
    return () => { alive = false; };
  }, [client, session.sessionId]);
  if (!client.putLocalSessionLink) return null;
  const save = async (audience: SessionLinkAudience) => {
    setBusy(true); setStatus(null);
    try { const next = await client.putLocalSessionLink!(session.sessionId, audience); setLink(next); return next; }
    catch { setStatus("Could not change the link. Try again."); return null; }
    finally { setBusy(false); }
  };
  const share = async () => {
    const next = link ?? await save("workspace");
    if (next?.url) setStatus(await copyText(next.url) ? "Link copied." : "Copy the link below.");
  };
  const off = async () => {
    setBusy(true);
    try { await client.deleteLocalSessionLink?.(session.sessionId); setLink(null); setStatus("Link turned off."); }
    catch { setStatus("Could not turn the link off. Try again."); }
    finally { setBusy(false); }
  };
  return <div className="flex flex-col gap-2 text-sm">
    <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void share()}><Link2 aria-hidden="true" />{link ? "Copy link" : "Share"}</Button>
    {link && <>
      <label htmlFor={audienceId}>Who can open it</label>
      <select id={audienceId} className="room-input w-auto" value={link.audience} disabled={busy} onChange={e => void save(e.target.value as SessionLinkAudience)}>
        <option value="workspace">Workspace only</option>
        <option value="anyone">Anyone with the link</option>
      </select>
      {link.audience === "anyone" && <p role="note" className="session-setting-warning">
        Anyone with the link can read this conversation and continue it in their Claude Code. Likely secrets are redacted for them; anything else in it is not.
      </p>}
      {link.audience === "anyone" && session.pickup?.privateRepo && <p className="session-setting-warning">This conversation includes code Claude read from your private repository.</p>}
      {session.provider === "claude" && <p className="session-setting-about">Teammates with the link can pick it up with your uncommitted changes, unredacted. Files your repository ignores are never uploaded. Kept until 7 days after the session ends.</p>}
      {link.url && <input aria-label="Session link" readOnly className="room-input font-mono text-xs" value={link.url} onFocus={e => e.currentTarget.select()} />}
      <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => void off()}>Turn off link</Button>
    </>}
    {status && <p role="status">{status}</p>}
  </div>;
}
```

`SessionDetails.tsx`:
- Import `SessionShareLink` (and drop `SessionShareLinks`); `Client` uses `Parameters<typeof SessionShareLink>[0]["client"]`.
- Import `useSessionAudit` and `auditLine` from `@lightbulb/shared`; drop `useSessionPickupPermission`.
- In `useAccessSwitches`, remove `pickup`. In `Access`, remove `pickup` from the destructuring.
- Delete the pick-up `Setting` block and the "Codex sessions cannot be picked up yet." line (`:157-165`).
- Replace the "Read-only snapshot link" block (`:169-175`) with:

```tsx
    <div className="session-setting">
      <div className="session-setting-text">
        <p className="session-setting-label">Link</p>
        <p className="session-setting-about">One link to this session. You choose who can open it; you can turn it off at any time.</p>
        {session.pickup?.allowed && <p role="status" className="session-setting-state">{pickupStatus(session.pickup, timeAgo) || "Taking the first snapshot."}</p>}
      </div>
      <SessionShareLink session={session} client={client} />
    </div>
    <Copies session={session} client={client} nameOf={nameOf} />
```

with, above `Access`:

```tsx
/** Who took a copy and when: the sharer's view of Continue (spec §2.1 step 4). */
function Copies({ session, client, nameOf }: { session: LocalSession; client: Client; nameOf: (userId: string) => string }) {
  const entries = useSessionAudit(client, session.sessionId, session.latestEventSeq).filter(e => e.action === "session_copied");
  if (!entries.length) return null;
  return <section aria-label="Copies" className="session-setting-text">
    <p className="session-setting-label">Copies taken</p>
    <ul>{entries.map(e => <li key={e.at + (e.actorUserId ?? "")}>{auditLine(e, nameOf)} · {timeAgo(e.at)}</li>)}</ul>
  </section>;
}
```

`LocalSession` has no `revision` field. `useSessionAudit`'s third argument only triggers a re-fetch, so pass `session.latestEventSeq` in place of `session.revision` above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd packages/shared && pnpm vitest run src/messaging && pnpm tsc --noEmit -p .`
Run: `cd apps/frontend && pnpm vitest run src/components/messaging && pnpm tsc --noEmit`
Expected: PASS. tsc fails on `src/app/chat/shared/page.tsx` (it uses the removed client methods). Delete that page in this task: `git rm apps/frontend/src/app/chat/shared/page.tsx`. Task 10 replaces it with `/s`.

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src apps/frontend/src/components/messaging
git rm apps/frontend/src/components/messaging/SessionShareLinks.tsx apps/frontend/src/components/messaging/__tests__/SessionShareLinks.test.tsx apps/frontend/src/app/chat/shared/page.tsx
git commit -m "feat(web): one Share link per session with an audience; Access lists copies"
```

---

### Task 10: The `/s` link page, the CLI confirm, and onboarding `returnTo`

**Files:**
- Create: `apps/frontend/src/app/s/page.tsx`, `apps/frontend/src/app/s/__tests__/page.test.tsx`
- Create: `apps/frontend/src/lib/returnTo.ts`, `apps/frontend/src/lib/__tests__/returnTo.test.ts`
- Modify: `apps/frontend/src/app/onboarding/page.tsx:144-148`

**Interfaces:**
- Consumes: Task 3 `POST /public/session-links/open|events` (raw `fetch`, because `useApi` refuses to call without a token, `packages/shared/src/api/client.ts:141-144`); Task 9 `createSessionCopy`, `SessionLinkPage`; `BACKEND_URL` (`lib/api.ts:19`); `sessionPermalink` (`messageLinks.ts:11`); `NativeSessionTranscript`.
- Produces: `safeReturnTo(raw: string | null): string`; `parseConfirm(search: string): { port: number; challenge: string } | null` (exported from the page module); sessionStorage key `lightbulb:session-link`.

- [ ] **Step 1: Write the failing tests**

`src/lib/__tests__/returnTo.test.ts`:

```ts
import { expect, it } from "vitest";
import { safeReturnTo } from "../returnTo";

it("keeps only same-origin paths", () => {
  expect(safeReturnTo("/s?port=5000")).toBe("/s?port=5000");
  for (const bad of [null, "", "https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"]) {
    expect(safeReturnTo(bad)).toBe("/chat");
  }
});
```

`src/app/s/__tests__/page.test.tsx` (mock pattern from `src/app/join/__tests__/page.test.tsx:1-60`):

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const createSessionCopy = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), BACKEND_URL: "https://api.test/api/v1", useApi: () => ({}) }));
vi.mock("@lightbulb/shared", async (orig) => ({ ...(await orig<typeof import("@lightbulb/shared")>()), createMessagingClient: () => ({ createSessionCopy }) }));
const clerk = vi.hoisted(() => ({ isLoaded: true, isSignedIn: false, orgId: null as string | null }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.isLoaded, isSignedIn: clerk.isSignedIn, orgId: clerk.orgId, getToken: async () => (clerk.isSignedIn ? "jwt" : null) }),
  SignUpButton: ({ children }: { children: React.ReactNode }) => children,
}));
const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/components/messaging/NativeSessionTranscript", () => ({ NativeSessionTranscript: ({ events }: { events: { text: string }[] }) => <div>{events.map(e => e.text).join("|")}</div> }));

import LinkPage, { go, parseConfirm } from "../page";

const TOKEN = "t".repeat(43);
const PAGE = { title: "Fix login", ownerFirstName: "Prasiddha", provider: "claude", state: "ended", continuable: true,
  code: { visibility: "private" }, me: { signedIn: false, member: false } };
const fetchMock = vi.fn();

function answer(page = PAGE) {
  fetchMock.mockImplementation(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (url.endsWith("/open") ? page : { events: [{ seq: 1, text: "hello" }], nextAfterSeq: null }),
  }));
}

describe("/s", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset(); createSessionCopy.mockReset(); push.mockReset();
    Object.assign(clerk, { isSignedIn: false, orgId: null });
    window.sessionStorage.clear();
    window.history.replaceState(null, "", `/s#${TOKEN}`);
  });

  it("lifts the token out of the URL, posts it in the body, and shows the ended page", async () => {
    answer();
    render(<LinkPage />);
    expect(await screen.findByText("Fix login")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ token: TOKEN });
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
    expect(screen.getByText("Code is in a private repository")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign up to continue" })).toBeInTheDocument();
  });

  it("a signed-in person with no workspace is sent to onboarding and back", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: null });
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    render(<LinkPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Create your free workspace" }));
    expect(push).toHaveBeenCalledWith("/onboarding?returnTo=%2Fs");
  });

  it("shows the command for a signed-in person with a workspace", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: "org_new" });
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    render(<LinkPage />);
    expect(await screen.findByText(`lightbulb continue '${window.location.origin}/s#${TOKEN}'`)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Get Lightbulb for Mac" })).toHaveAttribute("href", "/api/download/desktop");
  });

  it("in CLI mode, Continue makes the copy and hands the code to the loopback only", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: "org_new" });
    window.history.replaceState(null, "", `/s?port=53123&challenge=${"c".repeat(43)}#${TOKEN}`);
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    createSessionCopy.mockResolvedValue({ code: "k".repeat(43) });
    const to = vi.spyOn(go, "to").mockImplementation(() => {});
    render(<LinkPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Continue in Claude Code" }));
    expect(createSessionCopy).toHaveBeenCalledWith(TOKEN, "c".repeat(43));
    expect(to).toHaveBeenCalledWith(`http://127.0.0.1:53123/done?code=${"k".repeat(43)}`);
  });

  it("refuses a confirm with a bad port or challenge", () => {
    expect(parseConfirm(`?port=53123&challenge=${"c".repeat(43)}`)).toEqual({ port: 53123, challenge: "c".repeat(43) });
    for (const bad of ["?port=80&challenge=" + "c".repeat(43), "?port=99999&challenge=" + "c".repeat(43), "?port=5000&challenge=short", "?port=5000.5&challenge=" + "c".repeat(43)]) {
      expect(parseConfirm(bad)).toBeNull();
    }
  });

  it("a closed link says so", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, json: async () => ({ code: "LOCAL_SESSION_NOT_FOUND", message: "This link was turned off or is not for you." }) });
    render(<LinkPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This link was turned off or is not for you.");
  });
});
```

jsdom cannot navigate, so the page routes the loopback redirect through `go.to`, which the test spies on.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/returnTo.test.ts src/app/s/__tests__/page.test.tsx`
Expected: FAIL with `Cannot find module '../returnTo'` / `'../page'`

- [ ] **Step 3: Write minimal implementation**

`src/lib/returnTo.ts`:

```ts
/** A same-origin path to come back to (an absolute path, never `//host` or `/\host`), else /chat. */
export function safeReturnTo(raw: string | null): string {
  return raw && raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/\\") ? raw : "/chat";
}
```

`onboarding/page.tsx:147`: replace `router.replace("/chat");` with `router.replace(safeReturnTo(new URLSearchParams(window.location.search).get("returnTo")));` and import `safeReturnTo` from `@/lib/returnTo`. Read `window.location` inside the effect rather than `useSearchParams`, which would need a Suspense boundary.

`src/app/s/page.tsx`:

```tsx
"use client";

import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { SignUpButton, useAuth } from "@clerk/nextjs";
import { createMessagingClient, type LocalSessionEvent, type SessionLinkCode, type SessionLinkPage } from "@lightbulb/shared";
import { BACKEND_URL, useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { NativeSessionTranscript } from "@/components/messaging/NativeSessionTranscript";
import { copyText, sessionPermalink } from "@/components/messaging/messageLinks";

// The link's credential lives in the fragment, kept in this tab across sign-up (Clerk's modal
// on this page) and onboarding, never in a URL a server sees (spec §3.7).
const KEY = "lightbulb:session-link";
const MAX_PAGES = 40;
const POLL_MS = 5000;
type Stash = { token: string; port?: number; challenge?: string };

export const go = { to: (url: string) => window.location.assign(url) };

/** The CLI's confirm mode: only an integer port 1024-65535 and a 43-character challenge. */
export function parseConfirm(search: string): { port: number; challenge: string } | null {
  const q = new URLSearchParams(search);
  const port = q.get("port") ?? "", challenge = q.get("challenge") ?? "";
  const n = /^\d{4,5}$/.test(port) ? Number(port) : 0;
  return n >= 1024 && n <= 65535 && /^[A-Za-z0-9_-]{43}$/.test(challenge) ? { port: n, challenge } : null;
}

function readStash(): Stash | null {
  try { return JSON.parse(window.sessionStorage.getItem(KEY) ?? "null") as Stash | null; } catch { return null; }
}

async function publicPost<T>(path: string, body: unknown, token: string | null): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new Error((json as { message?: string } | null)?.message || "Could not open this link. Try again.");
  return json as T;
}

function CodeLine({ code }: { code: SessionLinkCode }) {
  if (code.visibility === "public") return <p>{[code.remote, code.branch, code.commit?.slice(0, 7)].filter(Boolean).join(" · ")}</p>;
  if (code.visibility === "workspace") return <p>{code.remote}{code.branch ? ` on ${code.branch}` : ""}</p>;
  if (code.visibility === "private") return <p>Code is in a private repository</p>;
  return <p>Code not shared</p>;
}

export default function SessionLinkPageRoute() {
  const router = useRouter();
  const api = useApi();
  const client = useMemo(() => createMessagingClient(api), [api]);
  const { isLoaded, isSignedIn, orgId, getToken } = useAuth();
  const [stash, setStash] = useState<Stash | null>(null);
  const [page, setPage] = useState<SessionLinkPage | null>(null);
  const [events, setEvents] = useState<LocalSessionEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useLayoutEffect(() => {
    const fragment = decodeURIComponent(window.location.hash.slice(1));
    const confirm = parseConfirm(window.location.search);
    const next = fragment ? { token: fragment, ...(confirm ?? {}) } : readStash();
    window.history.replaceState(null, "", "/s");
    if (!next) { setError("This link is incomplete. Ask for it again."); return; }
    try { window.sessionStorage.setItem(KEY, JSON.stringify(next)); } catch { /* storage blocked: this visit only */ }
    setStash(next);
  }, []);

  useEffect(() => {
    if (!stash || !isLoaded) return;
    let alive = true;
    let after: number | null = 0;
    const pull = async (bearer: string | null) => {
      for (let n = 0; after !== null && n < MAX_PAGES; n += 1) {
        const next: { events: LocalSessionEvent[]; nextAfterSeq: number | null } = await publicPost("/public/session-links/events", { token: stash.token, afterSeq: after, limit: 100 }, bearer);
        if (!alive) return;
        setEvents(list => [...list, ...next.events]);
        after = next.nextAfterSeq ?? (next.events.at(-1)?.seq ?? after);
        if (next.nextAfterSeq === null) break;
      }
    };
    let timer: ReturnType<typeof setInterval> | undefined;
    (async () => {
      try {
        const bearer = isSignedIn ? await getToken() : null;
        const opened = await publicPost<SessionLinkPage>("/public/session-links/open", { token: stash.token }, bearer);
        if (!alive) return;
        setPage(opened);
        await pull(bearer);
        // Phase 1 has no public live view: a live session's page follows the transcript (spec §2.2).
        if (opened.state === "live") timer = setInterval(() => { void pull(bearer).catch(() => {}); }, POLL_MS);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : "Could not open this link. Try again.");
      }
    })();
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [stash, isLoaded, isSignedIn, getToken]);

  const confirmContinue = async () => {
    if (!stash?.port || !stash.challenge) return;
    setBusy(true); setError(null);
    try {
      const { code } = await client.createSessionCopy(stash.token, stash.challenge);
      window.sessionStorage.removeItem(KEY);
      go.to(`http://127.0.0.1:${stash.port}/done?code=${encodeURIComponent(code)}`);
    } catch { setError("Could not make the copy. Try again."); setBusy(false); }
  };

  if (error) return <main className="mx-auto max-w-3xl p-4"><p role="alert">{error}</p></main>;
  if (!page || !stash) return <main className="mx-auto max-w-3xl p-4"><p role="status">Opening…</p></main>;
  const command = `lightbulb continue '${window.location.origin}/s#${stash.token}'`;
  const canContinue = page.provider === "claude" && page.continuable;
  return <main className="mx-auto max-w-3xl p-4">
    <header className="mb-4">
      <h1 className="text-lg font-semibold">{page.title?.trim() || "A shared session"}</h1>
      <p className="text-sm text-muted-foreground">{page.ownerFirstName}&apos;s {page.provider === "claude" ? "Claude Code" : "Codex"} session · {page.state === "live" ? "Live" : page.state === "ended" ? "Ended" : "Offline"}</p>
      <CodeLine code={page.code} />
      {page.me.member && page.sessionId && <a href={sessionPermalink(page.sessionId)}>Open in Lightbulb</a>}
    </header>
    {canContinue && <section aria-label="Continue" className="mb-4 flex flex-col gap-2">
      {!isSignedIn ? <SignUpButton mode="modal" forceRedirectUrl="/s"><Button type="button">Sign up to continue</Button></SignUpButton>
        : !orgId ? <Button type="button" onClick={() => router.push(`/onboarding?returnTo=${encodeURIComponent("/s")}`)}>Create your free workspace</Button>
        : stash.port ? <>
            <p>Continue {page.title?.trim() || "this session"} in Claude Code on this computer?</p>
            <Button type="button" disabled={busy} onClick={() => void confirmContinue()}>Continue in Claude Code</Button>
          </>
        : <>
            <a href="/api/download/desktop">Get Lightbulb for Mac</a>
            <p>Then run this in your terminal:</p>
            <code className="font-mono text-xs">{command}</code>
            <Button type="button" variant="outline" size="sm" onClick={() => void copyText(command)}>Copy command</Button>
          </>}
    </section>}
    <NativeSessionTranscript provider={page.provider} events={events} busy={false} ownerName={page.ownerFirstName} participants={[]} />
  </main>;
}
```

Check the `copyText` and `sessionPermalink` import paths against `components/messaging/messageLinks.ts`, and `NativeSessionTranscript`'s props against the deleted `/chat/shared` page (`provider`, `events`, `busy`, `ownerName`, `participants`). `/s` is outside `middleware.ts:13`'s protected matcher, so no middleware change is needed. Confirm this by reading the matcher. Next prefers the static `/s` route over `[seoSlug]`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/lib/__tests__/returnTo.test.ts src/app/s/__tests__/page.test.tsx src/app/onboarding && pnpm tsc --noEmit && pnpm lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/app/s apps/frontend/src/lib/returnTo.ts apps/frontend/src/lib/__tests__/returnTo.test.ts apps/frontend/src/app/onboarding/page.tsx
git commit -m "feat(web): the /s session link page, sign-up to continue, and the CLI confirm"
```

---

## After the tasks

- Dev acceptance (no E2E suite; an agent drives Chrome against dev; see `docs/runbooks/local-dev-browser-verification.md`). Do these steps in order:
  1. Share a Claude session as Anyone.
  2. Open `/s` signed out: the page reads, the transcript is redacted, and there is no id in the network panel.
  3. Sign up a fresh `+clerk_test` account, go through onboarding, and return to `/s`.
  4. On a Mac with the dev desktop build, run `lightbulb continue '<link>'`, then Confirm. Claude opens in the folder with the band.
  5. As the owner, see the copy listed under Access.
  6. Turn the link off. The page reads "turned off", and a new Continue fails.
- Ship the backend to prod before any desktop build that sends `repo` (Global Constraints).
- Delete this plan when Phase 1 ships (CLAUDE.md "Planning docs are disposable").
