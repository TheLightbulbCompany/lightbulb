# Hermes on AgentCore — M5 Browser + Shopify door (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Master plan:** `docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` — read its Goal, Architecture, Global Constraints and File Structure first; every task here inherits them. **Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md`.

**Milestone:** M5 Browser + Shopify door. Pass criteria: spec §6. Task numbering is local to this file.

---

## Tasks

### Task 1: Spike — verify agent-browser's native AgentCore provider from the spike runtime

**Files:** Create: `tmp/hermes_browser_probe.py` (throwaway, deleted in Step 5). Modify: none.
**Interfaces:** Consumes: `agent-browser` CLI (`npx agent-browser`) via subprocess; AWS creds via
`aws configure export-credentials --profile isol8-admin` (same pattern as the M2 spike's
`tmp/awssess.py`, per progress.md 23:00Z). Produces: a written-up finding (folded into this plan's
Task 2/4 and, if the provider cannot attach to a backend-started session, a follow-up fallback task
is added to this milestone — not written speculatively now).

- [ ] **Step 1: Write the probe script**
  ```python
  # tmp/hermes_browser_probe.py — one-shot, deleted after the spike (repo convention: no scratch
  # scripts survive past their run — see CLAUDE.md "Planning docs are disposable" / the deleted
  # migrate_secret_prefix.py precedent).
  import json, os, subprocess, sys, time
  import boto3

  REGION = "us-east-1"
  BROWSER_ID = "aws.browser.v1"

  def export_creds() -> None:
      out = subprocess.run(
          ["aws", "configure", "export-credentials", "--profile", "isol8-admin", "--format", "env"],
          capture_output=True, text=True, check=True,
      ).stdout
      for line in out.splitlines():
          if line.startswith("export "):
              k, _, v = line.removeprefix("export ").partition("=")
              os.environ[k] = v.strip('"')

  def backend_starts_a_session() -> dict:
      """Mirrors what POST /browser/session (Task 2) will do."""
      c = boto3.client("bedrock-agentcore", region_name=REGION)
      resp = c.start_browser_session(browserIdentifier=BROWSER_ID, name="hermes-probe", sessionTimeoutSeconds=300)
      return resp

  def agent_browser_attach(session_id: str) -> subprocess.CompletedProcess:
      env = dict(os.environ)
      env.update({
          "AGENT_BROWSER_PROVIDER": "agentcore",
          "AGENTCORE_REGION": REGION,
          "AGENTCORE_BROWSER_ID": BROWSER_ID,
          "AGENTCORE_SESSION_ID": session_id,  # UNDOCUMENTED — this is exactly what we're testing
      })
      return subprocess.run(
          ["npx", "-y", "agent-browser", "open", "https://example.com"],
          env=env, capture_output=True, text=True, timeout=60,
      )

  if __name__ == "__main__":
      export_creds()
      started = backend_starts_a_session()
      print("backend-started session:", json.dumps(started.get("sessionId")))
      time.sleep(2)
      result = agent_browser_attach(started["sessionId"])
      print("agent-browser stdout:", result.stdout[-4000:])
      print("agent-browser stderr:", result.stderr[-4000:])
      print("returncode:", result.returncode)
  ```
- [ ] **Step 2: Run it and record findings** Run: `cd /Users/prasiddhaparthsarthy/Desktop/lightbulb.nosync/.claude/worktrees/hermes-evaluation && python3 tmp/hermes_browser_probe.py`
  Expected: one of two outcomes, both concrete:
  (a) `agent-browser` accepts `AGENTCORE_SESSION_ID` (or another documented-but-unread-by-us env var)
  and attaches to the backend-started session — stderr prints a Live View URL containing the SAME
  `sessionId` `started["sessionId"]` printed on the line above; or
  (b) `agent-browser` starts its OWN session regardless (stderr prints a DIFFERENT session id). This
  is the expected outcome per the `agent-browser.dev/providers/agentcore` docs (no attach-to-existing
  option is documented) — in which case Task 4's design (bridge-owns-the-session,
  backend-reads-it-back) is confirmed as the only viable shape and Task 2's "reuse" path degrades to
  "the backend's own proactively-started session is used only until the agent's first browser call
  supersedes it," which Task 2 Step 3 must record as a comment.
- [ ] **Step 3: If (a), no further action.** If (b) — the common case — this is not a test failure,
  it is the answer this spike exists to get. Do not write a fallback provider now (the loopback
  SigV4-relay plugin was explicitly cut from this plan); only write one later if a real session-reuse
  bug is filed against the shipped panel.
- [ ] **Step 4: Stop the probe session (cleanup)**
  ```python
  boto3.client("bedrock-agentcore", region_name="us-east-1").stop_browser_session(
      browserIdentifier="aws.browser.v1", sessionId=started["sessionId"]
  )
  ```
- [ ] **Step 5: Delete the scratch script and commit the finding as a one-line spec update**
  ```bash
  rm tmp/hermes_browser_probe.py
  git add -A
  git commit -m "$(cat <<'EOF'
spike(hermes): agent-browser AgentCore provider always starts its own session (no attach)

Confirmed via tmp/hermes_browser_probe.py (deleted): agent-browser's AgentCore provider
starts a fresh session even when pointed at an existing sessionId. Task 2's proactive
POST /browser/session start is therefore a convenience for "open the panel before the
agent browses" only — the row is authoritative on whichever session most recently
reported in (backend-started or bridge-reported), per Task 4.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 2: Backend — `core/services/browser_sessions.py` + containers-row fields + `POST /api/v1/browser/session`

**Files:** Create: `apps/backend/core/services/browser_sessions.py`,
`apps/backend/tests/unit/services/test_browser_sessions.py`. Modify:
`apps/backend/routers/container.py` (add route near the other container-lifecycle routes, after
`container_wake_endpoint` at line 585).
**Interfaces:** Consumes: `core.repositories.container_repo.get_by_owner_id`,
`container_repo.update_fields` (both already exist, `apps/backend/core/repositories/container_repo.py:46,1608`);
`core.auth.resolve_owner_id`, `get_current_user`. Produces:
`async def ensure_browser_session(owner_id: str) -> BrowserSessionInfo` (dataclass:
`session_id, browser_id, profile_id, live_view_url, view_port, expires_at`);
route `POST /api/v1/browser/session` → `{sessionId, liveViewUrl, viewPort, expiresAt}` (camelCase
JSON, matching every other endpoint in `container.py`).

- [ ] **Step 1: Write the failing test** (pure — no live AWS; boto3 calls monkeypatched)
  ```python
  # apps/backend/tests/unit/services/test_browser_sessions.py
  from unittest.mock import MagicMock, patch
  import pytest

  from core.services import browser_sessions

  OWNER = "owner_1"


  def _row(**over):
      row = {"owner_id": OWNER, "status": "running"}
      row.update(over)
      return row


  @pytest.mark.asyncio
  async def test_starts_a_fresh_session_when_row_has_none():
      client = MagicMock()
      client.start_browser_session.return_value = {"sessionId": "sess-1", "browserIdentifier": "aws.browser.v1"}
      with (
          patch("core.services.browser_sessions.container_repo.get_by_owner_id", return_value=_row()),
          patch("core.services.browser_sessions.container_repo.update_fields", return_value=_row()),
          patch("core.services.browser_sessions._data_plane_client", return_value=client),
          patch("core.services.browser_sessions._presign_live_view_url", return_value="https://signed.example/x"),
      ):
          info = await browser_sessions.ensure_browser_session(OWNER)
      client.start_browser_session.assert_called_once()
      assert info.session_id == "sess-1"
      assert info.live_view_url == "https://signed.example/x"


  @pytest.mark.asyncio
  async def test_reuses_an_unexpired_row_session_without_calling_start():
      import time

      row = _row(
          browser_id="aws.browser.v1",
          browser_session_id="sess-existing",
          browser_session_expires_at=time.time() + 600,
      )
      client = MagicMock()
      client.get_browser_session.return_value = {"status": "READY"}
      with (
          patch("core.services.browser_sessions.container_repo.get_by_owner_id", return_value=row),
          patch("core.services.browser_sessions.container_repo.update_fields", return_value=row),
          patch("core.services.browser_sessions._data_plane_client", return_value=client),
          patch("core.services.browser_sessions._presign_live_view_url", return_value="https://signed.example/y"),
      ):
          info = await browser_sessions.ensure_browser_session(OWNER)
      client.start_browser_session.assert_not_called()
      assert info.session_id == "sess-existing"


  def test_presign_live_view_url_shape():
      from botocore.credentials import Credentials

      creds = Credentials("AKIA_TEST", "secret", "token")
      url = browser_sessions._presign_live_view_url(
          credentials=creds, region="us-east-1", browser_id="aws.browser.v1", session_id="sess-1"
      )
      assert url.startswith(
          "https://bedrock-agentcore.us-east-1.amazonaws.com/browser-streams/aws.browser.v1/sessions/sess-1/live-view?"
      )
      assert "X-Amz-Signature=" in url
      assert "X-Amz-Security-Token=token" in url
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_browser_sessions.py -q`
  Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.browser_sessions'`
- [ ] **Step 3: Write minimal implementation**
  ```python
  # apps/backend/core/services/browser_sessions.py
  """AgentCore Browser session lifecycle for the workspace browser panel.

  State is FOUR attributes on the containers row (spec §4.7, ponytail cut 2026-09-16 —
  no new table): browser_id, browser_session_id, browser_profile_id,
  browser_session_expires_at. Whichever party last touched the row (this module's
  ensure_browser_session, or the bridge's hook-token report in M5 Task 4) is the
  session the panel shows next — Task 1's spike found agent-browser always starts
  its OWN session, so "reuse" here is best-effort: it saves a StartBrowserSession
  call when the row's session is still fresh, not a hard guarantee the agent will
  use the same one.
  """

  from __future__ import annotations

  import dataclasses
  import time

  from core.config import settings
  from core.repositories import container_repo

  BROWSER_ID = "aws.browser.v1"  # the shared system browser tool; no execution role needed
  SESSION_TIMEOUT_S = 900  # AgentCore default; matches the panel's refresh-before-expiry timer
  LIVE_VIEW_URL_TTL_S = 300


  @dataclasses.dataclass
  class BrowserSessionInfo:
      session_id: str
      browser_id: str
      profile_id: str | None
      live_view_url: str
      view_port: dict
      expires_at: float


  def _data_plane_client():
      import boto3

      return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


  def _control_plane_client():
      import boto3

      return boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)


  def _presign_live_view_url(*, credentials, region: str, browser_id: str, session_id: str) -> str:
      """SigV4 query-presign the live-view stream endpoint. Not an API operation boto3's
      generic `generate_presigned_url` can sign (that only presigns defined operations,
      e.g. GetBrowserSession itself) — this presigns the raw DCV stream URL the docs
      describe (report 05 §1 "Live view"), the same technique this branch's own spike
      already proved against the runtime's /ws door (progress.md 23:00Z)."""
      from botocore.auth import SigV4QueryAuth
      from botocore.awsrequest import AWSRequest

      url = f"https://bedrock-agentcore.{region}.amazonaws.com/browser-streams/{browser_id}/sessions/{session_id}/live-view"
      request = AWSRequest(method="GET", url=url)
      SigV4QueryAuth(credentials, "bedrock-agentcore", region, expires=LIVE_VIEW_URL_TTL_S).add_auth(request)
      return request.url


  def _ensure_profile(owner_id: str, row: dict) -> str | None:
      """Create the owner's Browser Profile once (idempotent enough for the MVP: a lost
      profile_id just means a fresh empty profile next time — logins re-prompt, nothing
      breaks). Returns the profile id, or None if creation fails (never blocks the panel
      on a profile-store hiccup)."""
      if row.get("browser_profile_id"):
          return row["browser_profile_id"]
      try:
          resp = _control_plane_client().create_browser_profile(
              name=f"lightbulb-{owner_id}"[:48].replace("_", "-")
          )
          return resp.get("profileId") or resp.get("profileArn")
      except Exception:  # noqa: BLE001 — profile is a nice-to-have login persistence layer
          return None


  async def ensure_browser_session(owner_id: str) -> BrowserSessionInfo:
      row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
      client = _data_plane_client()
      now = time.time()

      session_id = row.get("browser_session_id")
      browser_id = row.get("browser_id") or BROWSER_ID
      still_fresh = bool(session_id) and float(row.get("browser_session_expires_at") or 0) > now + 30
      if still_fresh:
          try:
              status = client.get_browser_session(browserIdentifier=browser_id, sessionId=session_id).get("status")
              still_fresh = status in ("READY", "ACTIVE")
          except Exception:  # noqa: BLE001 — a dead/expired session falls through to a fresh start
              still_fresh = False

      profile_id = _ensure_profile(owner_id, row)

      if not still_fresh:
          kwargs = {"browserIdentifier": browser_id, "name": f"owner-{owner_id}"[:48], "sessionTimeoutSeconds": SESSION_TIMEOUT_S}
          if profile_id:
              kwargs["profileConfiguration"] = {"profileIdentifier": profile_id}
          resp = client.start_browser_session(**kwargs)
          session_id = resp["sessionId"]
          browser_id = resp.get("browserIdentifier", browser_id)
          expires_at = now + SESSION_TIMEOUT_S
      else:
          expires_at = float(row["browser_session_expires_at"])

      view_port = {"width": 1280, "height": 800}
      session = client.get_browser_session(browserIdentifier=browser_id, sessionId=session_id)
      view_port = session.get("viewPort") or view_port

      import boto3

      credentials = boto3.Session().get_credentials().get_frozen_credentials()
      live_view_url = _presign_live_view_url(
          credentials=credentials, region=settings.AWS_REGION, browser_id=browser_id, session_id=session_id
      )

      await container_repo.update_fields(
          owner_id,
          {
              "browser_id": browser_id,
              "browser_session_id": session_id,
              "browser_profile_id": profile_id,
              "browser_session_expires_at": expires_at,
          },
      )
      return BrowserSessionInfo(
          session_id=session_id, browser_id=browser_id, profile_id=profile_id,
          live_view_url=live_view_url, view_port=view_port, expires_at=expires_at,
      )
  ```
  Route addition in `apps/backend/routers/container.py` (after `container_wake_endpoint`, ~line 583):
  ```python
  from core.services import browser_sessions


  @router.post("/browser/session")
  async def start_browser_session(auth: AuthContext = Depends(get_current_user)) -> dict:
      owner_id = resolve_owner_id(auth)
      info = await browser_sessions.ensure_browser_session(owner_id)
      return {
          "sessionId": info.session_id,
          "liveViewUrl": info.live_view_url,
          "viewPort": info.view_port,
          "expiresAt": info.expires_at,
      }
  ```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_browser_sessions.py -q`
  Expected: `3 passed`
- [ ] **Step 5: Commit**
  ```bash
  git add apps/backend/core/services/browser_sessions.py apps/backend/tests/unit/services/test_browser_sessions.py apps/backend/routers/container.py
  git commit -m "$(cat <<'EOF'
feat(hermes): AgentCore Browser session service + POST /browser/session

Session state is 4 attributes on the containers row (spec §4.7). Live-view URL
is a hand-rolled SigV4 query-presign against the DCV stream endpoint — boto3's
generate_presigned_url only signs defined API operations, not this raw stream
URL.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 3: Infra — IAM for AgentCore Browser actions, NAT-vs-endpoint check

**Files:** Modify: `apps/infra/lib/stacks/service-stack.ts` (backend task role, near
`AgentCoreRuntimeList` at line ~530), `apps/infra/lib/stacks/container-stack.ts` (per-owner role,
near `AgentCoreWorkloadToken` at line ~386). Test: `apps/infra/test/service-stack.test.ts`,
`apps/infra/test/container-stack.test.ts` (extend existing IAM-assertion tests — `ls apps/infra/test`
for the exact file names before editing).
**Interfaces:** Produces: two new `iam.PolicyStatement`s (no new resources).

- [ ] **Step 1: Write the failing test** — extend whichever existing test file asserts the backend
  task role's policy statements (grep `AgentCoreRuntimeManage` in `apps/infra/test/*.test.ts` for the
  exact file and assertion style before writing this — do not create a parallel test file).
  ```typescript
  // appended to the existing AgentCore IAM test block
  test("backend task role can manage AgentCore Browser sessions", () => {
    template.hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Sid: "AgentCoreBrowserManage",
            Action: Match.arrayWith(["bedrock-agentcore:StartBrowserSession", "bedrock-agentcore:GetBrowserSession"]),
          }),
        ]),
      },
    });
  });
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/infra && npx jest test/service-stack.test.ts -t "AgentCore Browser"`
  Expected: FAIL — no matching statement found.
- [ ] **Step 3: Write minimal implementation**
  In `service-stack.ts`, next to the existing `AgentCoreRuntimeList` statement (~line 530):
  ```typescript
  this.backendTaskRole.addToPolicy(
    new iam.PolicyStatement({
      sid: "AgentCoreBrowserManage",
      actions: [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        "bedrock-agentcore:UpdateBrowserStream",
        "bedrock-agentcore:CreateBrowserProfile",
        "bedrock-agentcore:GetBrowserProfile",
        "bedrock-agentcore:SaveBrowserSessionProfile",
      ],
      // The system browser (aws.browser.v1) is shared across every owner — there is no
      // per-owner ARN to scope to (unlike the S3 replica prefix). Isolation is enforced
      // by application logic: the backend only ever reads/writes the session id on the
      // CALLING owner's own containers row (browser_sessions.ensure_browser_session).
      resources: [
        `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser/aws.browser.v1`,
        `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser-profile/*`,
      ],
    }),
  );
  ```
  In `container-stack.ts`, next to `AgentCoreWorkloadToken` (~line 386) — the bridge, running as the
  per-owner role, needs to START its own session (Task 4) and attach automation:
  ```typescript
  new iam.PolicyStatement({
    sid: "AgentCoreBrowserUse",
    actions: [
      "bedrock-agentcore:StartBrowserSession",
      "bedrock-agentcore:GetBrowserSession",
      "bedrock-agentcore:ConnectBrowserAutomationStream",
      "bedrock-agentcore:SaveBrowserSessionProfile",
    ],
    resources: [
      `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser/aws.browser.v1`,
      `arn:aws:bedrock-agentcore:${this.region}:${this.account}:browser-profile/*`,
    ],
  }),
  ```
  No VPC change: the VPC already has `natGateways: 1` with private-subnet egress
  (`network-stack.ts:36-53`), and no interface VPC endpoint exists anywhere in this repo today
  (confirmed: `grep -rn "InterfaceVpcEndpoint\|addInterfaceEndpoint" apps/infra/lib/stacks/*.ts` finds
  nothing — only the S3/DynamoDB Gateway endpoints). Per report 05 §1, `bedrock-agentcore-control` is
  not PrivateLink-eligible anyway, so a control-plane call (CreateBrowserProfile) needs NAT
  regardless; the data-plane calls this task adds (`StartBrowserSession` etc.) ARE PrivateLink-
  eligible but NAT already reaches them today. Skip the interface endpoint —
  ponytail: it is a cost/latency optimization on a path that already works, not a correctness
  requirement; add `com.amazonaws.${region}.bedrock-agentcore` in `network-stack.ts` only if dev
  usage shows NAT-egress cost or latency actually matters.
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/infra && npx jest test/service-stack.test.ts -t "AgentCore Browser"`
  Expected: PASS. Then: `cd apps/infra && npx cdk synth` — must exit 0, and if it rewrites
  `.github/workflows/deploy.yml`, commit that file in the same PR (CLAUDE.md's drift-guard rule).
- [ ] **Step 5: Commit**
  ```bash
  git add apps/infra/lib/stacks/service-stack.ts apps/infra/lib/stacks/container-stack.ts apps/infra/test/ .github/workflows/deploy.yml
  git commit -m "$(cat <<'EOF'
feat(hermes): IAM for AgentCore Browser — backend control, per-owner role automation

No new VPC interface endpoint: NAT egress already reaches bedrock-agentcore, and the
control-plane API isn't PrivateLink-eligible regardless (report 05 §1).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 4: Bridge — drive the browser per turn, report the session, save the profile

**Files:** Modify: `apps/infra/hermes/bridge/turn.py` (the per-turn module M1 split out of the spike's
`bridge.py`; every `bridge.py` reference below means `turn.py`). Modify:
`apps/backend/core/services/messaging.py` (add `BrowserSessionReport`/handler),
`apps/backend/routers/messaging.py` (add the hook-token route). Test:
`apps/infra/hermes/tests/test_browser_session_capture.py`,
`apps/backend/tests/unit/routers/test_messaging_browser_session.py`.
**Interfaces:** Produces: `POST /api/v1/internal/messaging/browser-session/{owner_id}` (hook-token,
mirrors `runs/{owner_id}/events` at `routers/messaging.py:586`) `{browserId, sessionId}` → `{"ok":
true}`. Pure helper: `_extract_browser_session(text: str) -> tuple[str, str] | None`.

- [ ] **Step 1: Write the failing test — the pure regex extractor (bridge side, no AWS)**
  ```python
  # apps/infra/hermes/tests/test_browser_session_capture.py
  from bridge import _extract_browser_session

  SAMPLE_STDERR = (
      "Live View URL: https://us-east-1.console.aws.amazon.com/"
      "bedrock-agentcore/browser/aws.browser.v1/session/abc-123-def#\n"
  )


  def test_extracts_browser_and_session_id():
      assert _extract_browser_session(SAMPLE_STDERR) == ("aws.browser.v1", "abc-123-def")


  def test_returns_none_when_no_browser_output():
      assert _extract_browser_session("nothing here") is None
  ```
  And the backend route test:
  ```python
  # apps/backend/tests/unit/routers/test_messaging_browser_session.py
  from fastapi.testclient import TestClient

  CONTAINER = {"owner_id": "owner_1", "hook_token": "tok"}


  def _client(monkeypatch):
      from fastapi import FastAPI
      import routers.messaging as mod

      async def _get(owner_id, consistent=False):
          return CONTAINER

      monkeypatch.setattr(mod.container_repo, "get_by_owner_id", _get)
      calls = {}

      async def _update(owner_id, fields):
          calls["fields"] = fields
          return {**CONTAINER, **fields}

      monkeypatch.setattr(mod.container_repo, "update_fields", _update)
      app = FastAPI()
      app.include_router(mod.router, prefix="/api/v1")
      return TestClient(app), calls


  def test_records_the_reported_session(monkeypatch):
      client, calls = _client(monkeypatch)
      resp = client.post(
          "/api/v1/internal/messaging/browser-session/owner_1",
          json={"browserId": "aws.browser.v1", "sessionId": "sess-9"},
          headers={"Authorization": "Bearer tok"},
      )
      assert resp.status_code == 200
      assert calls["fields"]["browser_session_id"] == "sess-9"


  def test_rejects_wrong_hook_token(monkeypatch):
      client, _ = _client(monkeypatch)
      resp = client.post(
          "/api/v1/internal/messaging/browser-session/owner_1",
          json={"browserId": "aws.browser.v1", "sessionId": "sess-9"},
          headers={"Authorization": "Bearer wrong"},
      )
      assert resp.status_code == 401
  ```
- [ ] **Step 2: Run tests to verify they fail**
  Run: `cd apps/infra/hermes && python3 -m pytest tests/test_browser_session_capture.py -q`
  Expected: FAIL with `ImportError: cannot import name '_extract_browser_session'`
  Run: `cd apps/backend && uv run pytest tests/unit/routers/test_messaging_browser_session.py -q`
  Expected: FAIL with 404 (route does not exist yet)
- [ ] **Step 3: Write minimal implementation**
  In `apps/infra/hermes/bridge/turn.py`, add near the top (module-level, no new deps):
  ```python
  import re

  _BROWSER_SESSION_RE = re.compile(
      r"bedrock-agentcore/browser/(?P<browser_id>[^/]+)/session/(?P<session_id>[^#\s]+)"
  )


  def _extract_browser_session(text: str) -> tuple[str, str] | None:
      """Pull (browser_id, session_id) out of agent-browser's stderr Live View URL line
      (report 05 §1: "AgentCore prints a Live View URL to stderr")."""
      m = _BROWSER_SESSION_RE.search(text or "")
      return (m.group("browser_id"), m.group("session_id")) if m else None
  ```
  In `run_turn`, before constructing `AIAgent` (bridge.py ~line 200), set process-wide env once per
  turn (one bridge process per workspace — never multiplexed, so process-wide is correct, spec
  §4.6):
  ```python
  def _refresh_browser_env() -> None:
      import boto3

      creds = boto3.Session().get_credentials().get_frozen_credentials()
      os.environ.update({
          "AGENT_BROWSER_PROVIDER": "agentcore",
          "AGENTCORE_REGION": os.environ.get("AWS_REGION", "us-east-1"),
          "AGENTCORE_BROWSER_ID": "aws.browser.v1",
          "AWS_ACCESS_KEY_ID": creds.access_key,
          "AWS_SECRET_ACCESS_KEY": creds.secret_key,
          "AWS_SESSION_TOKEN": creds.token or "",
      })
  ```
  and, wrapping the `agent.run_conversation(...)` call, capture fd 2 (subprocess stderr is not
  visible to Python's `sys.stderr` object — `agent-browser` is a real child process):
  ```python
  import contextlib

  def _run_with_stderr_capture(fn):
      tmp_path = _profile_dir(agent_id) / ".browser_stderr.tmp"
      saved_fd = os.dup(2)
      with open(tmp_path, "w+b") as tmp:
          os.dup2(tmp.fileno(), 2)
          try:
              return fn()
          finally:
              os.dup2(saved_fd, 2)
              os.close(saved_fd)
              tmp.seek(0)
              captured = tmp.read().decode(errors="replace")
      tmp_path.unlink(missing_ok=True)
      found = _extract_browser_session(captured)
      if found:
          browser_id, session_id = found
          _post_backend(f"browser-session/{OWNER}", {"browserId": browser_id, "sessionId": session_id})
          try:
              _data_plane_save_session_profile(browser_id, session_id)
          except Exception as exc:  # noqa: BLE001 — best-effort persistence, never fails the turn
              log.warning("SaveBrowserSessionProfile failed: %s", exc)
      return None


  def _data_plane_save_session_profile(browser_id: str, session_id: str) -> None:
      import boto3

      profile_id = os.environ.get("AGENTCORE_PROFILE_ID")
      if not profile_id:
          return
      boto3.client("bedrock-agentcore", region_name=os.environ.get("AWS_REGION", "us-east-1")).save_browser_session_profile(
          profileIdentifier=profile_id, browserIdentifier=browser_id, sessionId=session_id
      )
  ```
  Wire `_refresh_browser_env()` at the top of `run_turn` and wrap the existing
  `result = agent.run_conversation(...)` call with `_run_with_stderr_capture(lambda: agent.run_conversation(...))`.
  Backend side — `apps/backend/core/services/messaging.py` (near `UsageReportRequest`):
  ```python
  class BrowserSessionReport(ContractModel):
      browser_id: str = Field(min_length=1, max_length=128)
      session_id: str = Field(min_length=1, max_length=128)


  async def handle_browser_session_report(owner_id: str, body: BrowserSessionReport) -> dict:
      from core.repositories import container_repo
      import time

      await container_repo.update_fields(
          owner_id,
          {
              "browser_id": body.browser_id,
              "browser_session_id": body.session_id,
              "browser_session_expires_at": time.time() + 900,
          },
      )
      return {"ok": True}
  ```
  `apps/backend/routers/messaging.py` (near `post_usage_report`, line ~592):
  ```python
  @router.post("/internal/messaging/browser-session/{owner_id}")
  async def post_browser_session(owner_id: str, body: BrowserSessionReport, request: Request):
      await _require_hook_token(owner_id, request)
      return await service.handle_browser_session_report(owner_id, body)
  ```
  (import `BrowserSessionReport` alongside the existing `RunEventRequest`/`UsageReportRequest`
  imports at the top of `messaging.py`.)
- [ ] **Step 4: Run tests to verify they pass**
  Run: `cd apps/infra/hermes && python3 -m pytest tests/test_browser_session_capture.py -q` → PASS
  Run: `cd apps/backend && uv run pytest tests/unit/routers/test_messaging_browser_session.py -q` → PASS
- [ ] **Step 5: Commit**
  ```bash
  git add apps/infra/hermes/bridge/turn.py apps/infra/hermes/tests/test_browser_session_capture.py \
    apps/backend/core/services/messaging.py apps/backend/routers/messaging.py \
    apps/backend/tests/unit/routers/test_messaging_browser_session.py
  git commit -m "$(cat <<'EOF'
feat(hermes): bridge drives AgentCore Browser per turn, reports the session, saves the profile

Rotating execution-role creds are exported into the process env once per turn (one bridge
process per workspace, never multiplexed — spec §4.6). agent-browser's session id is
recovered from its stderr Live View URL line via an fd-2 capture around the turn, since
it is a real child process Python's sys.stderr redirection cannot see.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 5: Frontend — `BrowserDockPanel` on `BrowserLiveView`, CSP

**Files:** Modify: `apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx`,
`apps/frontend/next.config.ts`. Delete:
`apps/frontend/src/components/workspace/panels/browserViewUrl.ts` (Task 7 handles this + the backend
deletion together — see that task; this task only stops referencing it). Modify:
`apps/frontend/package.json` (add `bedrock-agentcore`). Test:
`apps/frontend/src/components/workspace/panels/__tests__/BrowserDockPanel.test.tsx`.
**Interfaces:** Consumes: `bedrock-agentcore/browser/live-view`'s `BrowserLiveView` component;
`useApi().get("/browser/session")` → `{sessionId, liveViewUrl, viewPort, expiresAt}` (Task 2).

- [ ] **Step 1: Write the failing test**
  ```typescript
  // apps/frontend/src/components/workspace/panels/__tests__/BrowserDockPanel.test.tsx
  import { render, screen, waitFor } from "@testing-library/react";
  import { BrowserDockPanel } from "../BrowserDockPanel";

  jest.mock("bedrock-agentcore/browser/live-view", () => ({
    BrowserLiveView: ({ signedUrl }: { signedUrl: string }) => <div data-testid="live-view">{signedUrl}</div>,
  }));
  jest.mock("@/lib/api", () => ({
    ...jest.requireActual("@/lib/api"),
    useApi: () => ({ get: jest.fn().mockResolvedValue({ sessionId: "s1", liveViewUrl: "https://x", viewPort: { width: 1280, height: 800 }, expiresAt: Date.now() / 1000 + 900 }) }),
  }));

  test("renders BrowserLiveView with the backend-minted signed url", async () => {
    render(<BrowserDockPanel {...({} as any)} />);
    await waitFor(() => expect(screen.getByTestId("live-view")).toHaveTextContent("https://x"));
  });
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/frontend && pnpm test -- BrowserDockPanel`
  Expected: FAIL — `BrowserDockPanel` still renders the noVNC `RFB` mount, no `data-testid="live-view"`.
- [ ] **Step 3: Write minimal implementation**
  ```bash
  cd apps/frontend && pnpm add bedrock-agentcore
  ```
  ```tsx
  // apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx (full rewrite)
  "use client";

  import type { IDockviewPanelProps } from "dockview-react";
  import { useCallback, useEffect, useRef, useState } from "react";
  import { Chrome, RotateCcw } from "lucide-react";
  import { BrowserLiveView } from "bedrock-agentcore/browser/live-view";

  import { useApi } from "@/lib/api";

  type ViewState = "connecting" | "connected" | "disconnected";
  type Session = { sessionId: string; liveViewUrl: string; viewPort: { width: number; height: number }; expiresAt: number };

  // Refresh 30s before the presigned URL's SigV4 expiry (Task 2's LIVE_VIEW_URL_TTL_S=300) —
  // matches the margin pattern used elsewhere in this codebase for time-boxed credentials.
  const REFRESH_MARGIN_S = 30;

  export function BrowserDockPanel(_props: IDockviewPanelProps): React.ReactElement {
    const api = useApi();
    const [state, setState] = useState<ViewState>("connecting");
    const [session, setSession] = useState<Session | null>(null);
    const [attempt, setAttempt] = useState(0);
    const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const fetchSession = useCallback(async () => {
      setState("connecting");
      try {
        const info = await api.post<Session>("/browser/session", {});
        setSession(info);
        setState("connected");
        const delayMs = Math.max(5_000, (info.expiresAt - Date.now() / 1000 - REFRESH_MARGIN_S) * 1000);
        timerRef.current = setTimeout(() => setAttempt((n) => n + 1), delayMs);
      } catch {
        setState("disconnected");
      }
    }, [api]);

    useEffect(() => {
      void fetchSession();
      return () => {
        if (timerRef.current) clearTimeout(timerRef.current);
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [attempt]);

    const retry = useCallback(() => setAttempt((n) => n + 1), []);

    return (
      <div className="relative flex h-full w-full flex-col bg-[var(--panel)]">
        {session && state === "connected" && (
          <BrowserLiveView signedUrl={session.liveViewUrl} remoteWidth={session.viewPort.width} remoteHeight={session.viewPort.height} />
        )}
        {state !== "connected" && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-center">
            <Chrome size={28} className="text-[var(--midnight-muted)]" />
            {state === "connecting" ? (
              <p className="text-sm text-[var(--midnight-muted)]">Connecting to your agents&rsquo; browser…</p>
            ) : (
              <>
                <p className="max-w-xs text-sm text-[var(--midnight-muted)]">
                  Browser view isn&rsquo;t reachable. Retry once your agents are awake.
                </p>
                <button onClick={retry} className="flex items-center gap-1.5 rounded-md border border-[var(--line)] px-3 py-1.5 text-sm text-[var(--midnight)] transition-colors hover:bg-[var(--hover)]">
                  <RotateCcw size={14} />
                  Retry
                </button>
              </>
            )}
          </div>
        )}
      </div>
    );
  }
  ```
  Note: no take-control button (ponytail cut confirmed in the coordinator's message and spec
  §4.7 — "the DCV view already takes human input; a take-control switch waits until an agent/human
  input conflict is observed"). `next.config.ts` CSP (`connect-src`, line 65, and `worker-src`
  already covers `blob:` for the DCV client's workers):
  ```typescript
  // next.config.ts:65 — add the bedrock-agentcore data-plane host (DCV live-view + automation stream)
  `connect-src 'self' https://*.clerk.accounts.dev ${clerkOrigins.join(" ")} ${apiOrigin} https://api.stripe.com wss://*.appsync-realtime-api.us-east-1.amazonaws.com https://us.i.posthog.com https://us-assets.i.posthog.com https://app.termly.io wss://bedrock-agentcore.${process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1"}.amazonaws.com https://bedrock-agentcore.${process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1"}.amazonaws.com`,
  ```
  (This report-only header never blocks — verify the exact host/worker requirements with a
  throwaway page per report 05 §5 risk 4 before promoting `Content-Security-Policy-Report-Only` to
  enforcing; that promotion is out of scope here.)
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/frontend && pnpm test -- BrowserDockPanel`
  Expected: PASS
- [ ] **Step 5: Commit**
  ```bash
  git add apps/frontend/src/components/workspace/panels/BrowserDockPanel.tsx apps/frontend/next.config.ts apps/frontend/package.json apps/frontend/pnpm-lock.yaml apps/frontend/src/components/workspace/panels/__tests__/BrowserDockPanel.test.tsx
  git commit -m "$(cat <<'EOF'
feat(hermes): BrowserDockPanel renders AgentCore Browser via BrowserLiveView

No take-control button — the DCV live view already takes human input; add one only if an
agent/human input conflict is observed in practice (spec §4.7).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

### Task 6: Shopify door — port to the bridge, persist via profile sync, loopback-callback decision

**Files:** Modify: `apps/infra/hermes/bridge/server.py` (new sub-routes under `handle_plugin`; M1 moved the
route table there from the spike's `bridge.py`, so every `bridge.py` reference below means `server.py`). Create:
`apps/infra/hermes/tests/test_shopify.py`. Read-only reference (do not edit; branch not merged):
`feat/shopify-connections-row:apps/infra/openclaw/plugins/lightbulb-channel/src/shopify.ts`,
`feat/shopify-connections-row:apps/backend/core/services/shopify_login.py`.
**Interfaces:** Produces: `POST /plugins/lightbulb-channel/shopify/login {store, scopes}`,
`GET /plugins/lightbulb-channel/shopify/status?store=`, `POST /plugins/lightbulb-channel/shopify/logout
{store}` — same three routes `shopify_login.py`'s `_plugin()` already calls
(`git show feat/shopify-connections-row:apps/backend/core/services/shopify_login.py:80-97`), so
`shopify_login.py` needs ONLY its `_plugin()` resolver changed (container IP → `container_rpc.call`,
already runtime-agnostic per report 04 — no edit needed there at all once M1's RPC plumbing lands)
when that branch is rebased onto this one; not this task's job to rebase it.

- [ ] **Step 1: Write the failing test**
  ```python
  # apps/infra/hermes/tests/test_shopify.py
  from bridge import parse_authorize_url, sessions_file_path, normalize_scopes

  SAMPLE = "Open this URL manually:\nhttps://my-shop.myshopify.com/admin/oauth/authorize?client_id=x\n"


  def test_parse_authorize_url_pins_to_the_store():
      assert parse_authorize_url(SAMPLE, "my-shop.myshopify.com") == "https://my-shop.myshopify.com/admin/oauth/authorize?client_id=x"


  def test_parse_authorize_url_rejects_a_different_store():
      assert parse_authorize_url(SAMPLE, "other-shop.myshopify.com") is None


  def test_sessions_file_path_honors_xdg_config_home(tmp_path):
      env = {"XDG_CONFIG_HOME": str(tmp_path)}
      assert sessions_file_path(env) == str(tmp_path / "shopify-cli-store-nodejs" / "config.json")


  def test_normalize_scopes_defaults_to_the_read_set():
      assert normalize_scopes(None) == [
          "read_products", "read_orders", "read_customers", "read_inventory", "read_locations", "read_reports",
      ]


  def test_normalize_scopes_rejects_bad_handles():
      assert normalize_scopes(["not a scope!"]) is None
  ```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/infra/hermes && python3 -m pytest tests/test_shopify.py -q`
  Expected: FAIL with `ImportError: cannot import name 'parse_authorize_url'`
- [ ] **Step 3: Write minimal implementation** — Python port of
  `shopify.ts`'s pure functions (`git show feat/shopify-connections-row:apps/infra/openclaw/plugins/lightbulb-channel/src/shopify.ts:88-96,102-105,112-115`),
  added to `bridge.py`:
  ```python
  import os
  import re
  import subprocess
  import time

  STORE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}\.myshopify\.com$")
  SCOPE_RE = re.compile(r"^[a-z_]{1,64}$")
  DEFAULT_SCOPES = ["read_products", "read_orders", "read_customers", "read_inventory", "read_locations", "read_reports"]
  _shopify_logins: dict[str, dict] = {}  # store -> {process, output, authorize_url}


  def normalize_scopes(raw) -> list[str] | None:
      if raw is None:
          return list(DEFAULT_SCOPES)
      if not isinstance(raw, list) or not raw:
          return None
      scopes = [str(s).strip() for s in raw]
      return sorted(set(scopes)) if all(SCOPE_RE.match(s) for s in scopes) else None


  def parse_authorize_url(output: str, store: str) -> str | None:
      pattern = re.compile(rf"https://{re.escape(store)}/admin/oauth/authorize\?\S+")
      m = pattern.search(output)
      return m.group(0) if m else None


  def sessions_file_path(env: dict) -> str:
      base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", ""), ".config")
      return os.path.join(base, "shopify-cli-store-nodejs", "config.json")


  def _shopify_env(agent_id: str) -> dict:
      # Persistence via the EXISTING profile sync (bridge.py's restore_profile/save_profile
      # already rglobs the whole profile dir) — no separate S3 mechanism needed. This is the
      # entire "persist the CLI config dir" requirement.
      config_home = _profile_dir(agent_id) / ".shopify-config"
      config_home.mkdir(parents=True, exist_ok=True)
      env = dict(os.environ)
      env["XDG_CONFIG_HOME"] = str(config_home)
      return env


  def shopify_login(agent_id: str, store: str, scopes: list[str] | None) -> tuple[int, dict]:
      if not STORE_RE.match(store):
          return 400, {"ok": False, "error": {"code": "INVALID_STORE", "message": "bad store host"}}
      normalized_scopes = normalize_scopes(scopes)
      if normalized_scopes is None:
          return 400, {"ok": False, "error": {"code": "INVALID_SCOPES", "message": "bad scope list"}}
      env = _shopify_env(agent_id)
      proc = subprocess.Popen(
          ["shopify", "store", "auth", "--store", store, "--scopes", ",".join(normalized_scopes)],
          env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
      )
      output = ""
      deadline = time.time() + 20
      while time.time() < deadline:
          line = proc.stdout.readline()
          if not line:
              break
          output += line
          url = parse_authorize_url(output, store)
          if url:
              _shopify_logins[store] = {"process": proc, "output": output, "authorize_url": url, "agent_id": agent_id}
              return 200, {"ok": True, "authorizeUrl": url}
      return 502, {"ok": False, "error": {"code": "CLI_TIMEOUT", "message": "shopify CLI did not print an authorize URL"}}


  def shopify_status(agent_id: str, store: str) -> tuple[int, dict]:
      import json as _json

      pending = store in _shopify_logins
      path = _profile_dir(agent_id) / ".shopify-config" / "shopify-cli-store-nodejs" / "config.json"
      connected = False
      if path.exists():
          try:
              data = _json.loads(path.read_text())
              connected = any(k.endswith(f"::{store}") for k in data)
          except Exception:  # noqa: BLE001
              connected = False
      return 200, {"ok": True, "connected": connected, "pending": pending}


  def shopify_logout(agent_id: str, store: str) -> tuple[int, dict]:
      path = _profile_dir(agent_id) / ".shopify-config" / "shopify-cli-store-nodejs" / "config.json"
      if not path.exists():
          return 200, {"ok": True}
      import json as _json

      data = _json.loads(path.read_text())
      keys = [k for k in data if k.endswith(f"::{store}")]
      for k in keys:
          del data[k]
      path.write_text(_json.dumps(data, indent="\t"))
      _shopify_logins.pop(store, None)
      return 200, {"ok": True}
  ```
  Dispatch in `handle_plugin` (bridge.py, alongside the existing `rpc`/`inbound` branches):
  ```python
  if path.startswith("/plugins/lightbulb-channel/shopify/") and _authorized(headers):
      agent_id = "main"  # the CLI login is per-workspace, not per-agent — pinned to the default profile
      sub = path.rsplit("/", 1)[-1]
      if sub == "login" and method == "POST":
          return shopify_login(agent_id, body.get("store", ""), body.get("scopes"))
      if sub == "status" and method == "GET":
          store = (query or {}).get("store", [""])[0]
          return shopify_status(agent_id, store)
      if sub == "logout" and method == "POST":
          return shopify_logout(agent_id, body.get("store", ""))
  ```
  **Loopback-callback decision (spike, not coded here):** the CLI's OAuth redirect
  (`http://127.0.0.1:13387/auth/callback`) is loopback-bound to whichever host runs `shopify store
  auth` — the bridge's own container. Report 05 §4-5 identifies the crux: does the AgentCore
  Browser's Chrome (a SEPARATE microVM) share that loopback? It almost certainly does not. Two
  options, decided by a spike (not this task's coded output):
  (a) run `shopify store auth` on the bridge itself (same container as this Python process — it IS
      the loopback the CLI opens), and once Task 4's browser session exists, navigate it to the
      printed authorize URL over the CDP automation stream (`Page.navigate`) instead of opening a
      new tab via a container `browser.request` RPC that no longer exists;
  (b) the desktop node relay (M2's node route, once merged) proxies `127.0.0.1:13387` on the OWNER'S
      OWN MACHINE and the owner completes OAuth in their own browser instead of the shared AgentCore
      Browser — a real UX change from today (no change needed to the bridge's port at all, since the
      loopback is now the owner's Mac, which already runs the CLI's Chrome-adjacent browser).
  Decision: **(a)** — it needs no new M2 dependency and matches today's shape (Chrome navigates to
  the URL, not the owner's own machine); the CDP navigate is a two-line addition to `shopify_login`
  above (call `_data_plane_client().start_browser_session(...)` — already available via Task 2's
  session — then a CDP `Page.navigate` over `agent-browser`'s `--cdp` connection, or simply return
  the URL to `shopify_login.py`'s existing `container_rpc.call(owner_id, "browser.request", ...)`
  caller and let the RPC handler translate that into a CDP navigate once the RPC method table
  exists — a follow-up wired in whichever M1/M4 task adds `browser.request` to the bridge's RPC
  method table, not duplicated here). Record this decision in the spec's open-questions table when
  merging (§10).
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/infra/hermes && python3 -m pytest tests/test_shopify.py -q`
  Expected: `5 passed`
- [ ] **Step 5: Commit**
  ```bash
  git add apps/infra/hermes/bridge/server.py apps/infra/hermes/tests/test_shopify.py
  git commit -m "$(cat <<'EOF'
feat(hermes): port the Shopify CLI login door into the bridge

Python port of shopify.ts's pure functions (branch feat/shopify-connections-row, not
merged — ported by hand, not rebased). CLI config dir rides the existing profile S3
sync (XDG_CONFIG_HOME under the profile dir) — no new persistence mechanism. Loopback
callback: CLI runs on the bridge's own container (same loopback host as today's
per-owner OpenClaw container), authorize URL is navigated in the AgentCore Browser
session over CDP once the RPC method table carries browser.request (M1/M4).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
  ```

---

