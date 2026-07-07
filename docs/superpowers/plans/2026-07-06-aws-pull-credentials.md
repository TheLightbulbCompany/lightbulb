# AWS Pull Credentials (`credential_process`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken `credential_source=EcsContainer` delivery of the aws_sts rail with on-demand pull: a `credential_process` helper on the owner's EFS fetches short-lived connector credentials from a new bearer-token-authenticated backend endpoint that performs the `sts:AssumeRole` server-side.

**Architecture:** The materializer writes three files to the owner's `.aws/` dir (config with `credential_process`, a zero-dependency node helper, a bearer token file). The helper caches credentials and calls `POST /api/v1/connections/aws/credentials`; the backend validates the token (SHA-256 hash at rest in the existing SM secret), assumes the connector role with the server-side ExternalId, and returns credential_process JSON. Connector trust repoints from the per-owner container role to our account root + ExternalId; the per-owner `owner-aws-sts` IAM machinery and the boundary carve-out are retired.

**Tech Stack:** FastAPI, boto3 (STS/SM), pytest (+AsyncMock/MagicMock conventions), node ≥18 ESM (global `fetch`), AWS CDK (TypeScript) + jest.

**Spec:** `docs/superpowers/specs/2026-07-06-aws-backend-materialized-creds-design.md` — read it for rationale; this plan is the how.

## Global Constraints

- Profile name becomes exactly `isol8-connector` (`[profile isol8-connector]`) — Task 7 renames it in the materializer AND the image-baked plugin in lockstep (user pulled the image bump into scope). Tasks 1–6 keep the working name `isol8-readonly` that Task 2 shipped; Task 7 flips every site at once. Never `[default]`.
- Connector role name is exactly `isol8-connector` (`bedrock_aip.CONNECTOR_ROLE_NAME`); the CFN logical resource ID `Isol8ReadOnlyRole` must NOT be renamed (renaming a logical ID REPLACES the role in any stack updated with the new template; keep it with an explanatory comment). The template FILE renames to `aws-connector.yaml`.
- No new account-id literals: use `bedrock_aip.OUR_ACCOUNT_ID` (backend) / `this.account` (CDK).
- The bearer token is stored ONLY as a SHA-256 hex hash server-side (`cli_token_hash` in the SM credential); plaintext exists only in the owner's EFS `token` file.
- `DurationSeconds=3600` on AssumeRole (role-chaining cap; connector `MaxSessionDuration` is 3600).
- Plugin (`apps/infra/openclaw/plugins/aws-exec-profile/`) gets a NAMING-ONLY change (constants, descriptions, tests — Task 7); its hook wiring and resolver logic are untouched. The extended image rebuilds automatically on merge (`build-openclaw-image.yml` triggers on `plugins/**`). The OpenClaw FORK is UNCHANGED.
- `apps/infra/lib/app.ts` is UNCHANGED (its `PLATFORM_OPS_OWNER_ID_*` env plumbing feeds service-stack; leaving it untouched also avoids `deploy.yml` regeneration).
- Helper script: node ESM, zero dependencies, `credential_process = /usr/bin/env node …` (PATH-robust, no bare `node`, no guessed absolute node path).
- No PR/issue numbers in code comments (repo convention).
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Infra tests: `cd apps/infra && npx jest <path>`.

## File Map

| File | Change |
|---|---|
| `apps/backend/core/services/credentials/aws_cfn.py` | + `platform_trust_principal_arn()`; − `owner_task_role_arn()` (Task 4 removes last callers) |
| `apps/backend/core/services/credentials/materializers/aws_cli.py` | Rewrite: 3-file bundle (config / helper / token), remove() drops 4 files |
| `apps/backend/routers/connections.py` | + `POST /aws/credentials` endpoint + token rail; aws_complete/aws_setup/gate/disconnect rewiring |
| `apps/backend/core/billing/bedrock_aip.py` | − `_build_owner_aws_sts_doc`, `put_owner_aws_sts_policy`, `delete_owner_aws_sts_policy` |
| `apps/infra/lib/stacks/container-stack.ts` | Boundary: unconditional Deny, remove carve-out + `BoundaryStsAssumeRole` |
| `apps/infra/aws/aws-readonly-connector.yaml` → `aws-connector.yaml` + `README.md` | File rename + prose drift fixes (trust principal, three tiers) |
| `apps/infra/lib/stacks/service-stack.ts:879` | `CFN_TEMPLATE_KEY` → `aws-connector.yaml` + comment |
| `apps/infra/openclaw/plugins/aws-exec-profile/*` (6 files) | Naming-only: profile constant → `isol8-connector`, descriptions, tests |
| `apps/backend/core/services/credentials/providers.yaml` + `registry.py` + `core/containers/config.py:666` | Comment/docstring drift only |
| Tests | `test_aws_cfn.py` (update), `test_materializer_aws_cli.py` (rewrite), `test_aws_sts_grant.py` (DELETE), `test_connections.py` (update + new class), `apps/infra/test/container-stack.test.ts` (new assertion) |

Tasks 1–3 are independent of each other; Task 4 consumes 1+2+3; Task 5 requires 4; Tasks 6–7 anytime after 5; Task 8 last.

---

### Task 1: `platform_trust_principal_arn()` in aws_cfn

**Files:**
- Modify: `apps/backend/core/services/credentials/aws_cfn.py`
- Test: `apps/backend/tests/credentials/test_aws_cfn.py`

**Interfaces:**
- Consumes: `bedrock_aip.OUR_ACCOUNT_ID` (existing).
- Produces: `aws_cfn.platform_trust_principal_arn() -> str` returning `arn:aws:iam::877352799272:root`. Task 4 repoints `connections.py` callers to it and then deletes `owner_task_role_arn` — do NOT delete it in this task (it still has callers).

- [ ] **Step 1: Write the failing test**

Append to `apps/backend/tests/credentials/test_aws_cfn.py`:

```python
def test_platform_trust_principal_arn_is_account_root():
    # Account-root + ExternalId is the trust pattern every connector role pins.
    # Root never drifts, so connected accounts never need a trust update.
    arn = aws_cfn.platform_trust_principal_arn()
    assert arn == "arn:aws:iam::877352799272:root"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/credentials/test_aws_cfn.py::test_platform_trust_principal_arn_is_account_root -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'platform_trust_principal_arn'`

- [ ] **Step 3: Implement**

In `apps/backend/core/services/credentials/aws_cfn.py`, add below `gen_external_id()` (keep `owner_task_role_arn` for now — Task 4 removes it with its last callers):

```python
def platform_trust_principal_arn() -> str:
    """Account-root principal every connector role trusts — the standard
    cross-account SaaS pattern. Root delegates to our account's IAM; the
    per-owner ExternalId condition (server-side only) is the per-owner lock.
    The root ARN can never drift, so a connected account never needs a trust
    update again."""
    return f"arn:aws:iam::{bedrock_aip.OUR_ACCOUNT_ID}:root"
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd apps/backend && uv run pytest tests/credentials/test_aws_cfn.py -v`
Expected: ALL PASS (including the old `test_owner_task_role_arn_reuses_bedrock_aip_name` — that helper still exists until Task 4)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/aws_cfn.py apps/backend/tests/credentials/test_aws_cfn.py
git commit -m "feat(aws): platform_trust_principal_arn — account-root trust principal"
```

---

### Task 2: Materializer rewrite — config + helper + token bundle

**Files:**
- Modify: `apps/backend/core/services/credentials/materializers/aws_cli.py` (full rewrite)
- Test: `apps/backend/tests/credentials/test_materializer_aws_cli.py` (full rewrite)

**Interfaces:**
- Consumes: `atomic_write(path, data, *, mode=0o600)` and `owner_efs_root(owner_id)` from `materializers/base.py`; `settings.PUBLIC_API_BASE` (exists, per-env via CDK).
- Produces: `aws_cli.write(owner_id, provider_key, credential)` now REQUIRES `credential["cli_token"]` (plaintext) and `credential["region"]`; it ignores `role_arn`/`external_id`/`cli_token_hash` if present. Module constants `AWS_READONLY_PROFILE`, `HELPER_FILENAME = "isol8-creds.mjs"`, `TOKEN_FILENAME = "token"`, `CACHE_FILENAME = "cache.json"`. Task 4's `aws_complete` passes `{**stored_credential, "cli_token": plaintext}`.

- [ ] **Step 1: Rewrite the test file (failing tests)**

Replace the ENTIRE content of `apps/backend/tests/credentials/test_materializer_aws_cli.py` with:

```python
import http.server
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from core.services.credentials.materializers import aws_cli


def _cred() -> dict:
    return {
        "role_arn": "arn:aws:iam::111122223333:role/r",
        "external_id": "ext123",
        "region": "us-east-1",
        "cli_token_hash": "deadbeef",
        "cli_token": "tok_plain_abc",
    }


def _write(tmp_path, monkeypatch, owner="o") -> Path:
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "https://api-test.isol8.co")
    aws_cli.write(owner, "aws", _cred())
    return Path(tmp_path) / owner / ".aws"


def test_config_uses_credential_process_and_no_secrets(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    body = (aws_dir / "config").read_text()
    assert body == (
        "[profile isol8-readonly]\n"
        "region = us-east-1\n"
        "credential_process = /usr/bin/env node /home/node/.openclaw/.aws/isol8-creds.mjs\n"
    )
    # The config must carry no assume-role or secret material: a profile mixing
    # role_arn with credential_process would flip botocore into assume-role mode,
    # and external_id must never touch EFS again.
    for forbidden in ("role_arn", "external_id", "credential_source", "cli_token", "aws_access_key_id"):
        assert forbidden not in body


def test_named_profile_not_default_so_bedrock_chain_is_unaffected(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    body = (aws_dir / "config").read_text()
    assert body.startswith(f"[profile {aws_cli.AWS_READONLY_PROFILE}]\n")
    assert "[default]" not in body


def test_file_modes(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    assert oct((aws_dir / "config").stat().st_mode)[-3:] == "600"
    assert oct((aws_dir / aws_cli.HELPER_FILENAME).stat().st_mode)[-3:] == "700"
    assert oct((aws_dir / aws_cli.TOKEN_FILENAME).stat().st_mode)[-3:] == "600"


def test_token_file_holds_owner_and_plaintext_token(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch, owner="org_x")
    tok = json.loads((aws_dir / aws_cli.TOKEN_FILENAME).read_text())
    assert tok == {"owner_id": "org_x", "token": "tok_plain_abc"}


def test_helper_bakes_api_base_and_no_placeholders(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    helper = (aws_dir / aws_cli.HELPER_FILENAME).read_text()
    assert 'const API_BASE = "https://api-test.isol8.co";' in helper
    assert "__API_BASE__" not in helper


def test_remove_unlinks_all_artifacts_and_is_idempotent(tmp_path, monkeypatch):
    aws_dir = _write(tmp_path, monkeypatch)
    # Simulate a runtime cache the helper wrote.
    (aws_dir / aws_cli.CACHE_FILENAME).write_text("{}")
    aws_cli.remove("o", "aws", _cred())
    for name in ("config", aws_cli.HELPER_FILENAME, aws_cli.TOKEN_FILENAME, aws_cli.CACHE_FILENAME):
        assert not (aws_dir / name).exists()
    aws_cli.remove("o", "aws", _cred())  # second remove must not raise


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Stub credential endpoint: asserts bearer + owner and returns fixed creds."""

    calls: list[dict] = []
    creds = {
        "Version": 1,
        "AccessKeyId": "ASIATEST",
        "SecretAccessKey": "secret",
        "SessionToken": "session",
        "Expiration": "2099-01-01T00:00:00+00:00",
    }

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _StubHandler.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        payload = json.dumps(_StubHandler.creds).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # keep pytest output clean
        pass


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_end_to_end_fetch_then_cache(tmp_path, monkeypatch):
    """Run the ACTUAL materialized helper under node against a local stub:
    first invocation fetches + caches; second serves the cache (no new call)."""
    _StubHandler.calls = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
        monkeypatch.setattr(
            "core.config.settings.PUBLIC_API_BASE", f"http://127.0.0.1:{server.server_port}"
        )
        aws_cli.write("org_x", "aws", _cred())
        helper = Path(tmp_path) / "org_x" / ".aws" / aws_cli.HELPER_FILENAME

        r1 = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
        assert r1.returncode == 0, r1.stderr
        assert json.loads(r1.stdout) == _StubHandler.creds
        assert len(_StubHandler.calls) == 1
        assert _StubHandler.calls[0]["auth"] == "Bearer tok_plain_abc"
        assert _StubHandler.calls[0]["body"] == {"owner_id": "org_x"}

        r2 = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
        assert r2.returncode == 0, r2.stderr
        assert json.loads(r2.stdout) == _StubHandler.creds
        assert len(_StubHandler.calls) == 1  # cache hit — no second fetch
    finally:
        server.shutdown()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_helper_clear_error_when_backend_unreachable_and_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.settings.EFS_MOUNT_PATH", str(tmp_path))
    # Unroutable port — connection refused immediately.
    monkeypatch.setattr("core.config.settings.PUBLIC_API_BASE", "http://127.0.0.1:9")
    aws_cli.write("org_x", "aws", _cred())
    helper = Path(tmp_path) / "org_x" / ".aws" / aws_cli.HELPER_FILENAME
    r = subprocess.run(["node", str(helper)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "isol8" in r.stderr  # names us, not a bare stack trace
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/credentials/test_materializer_aws_cli.py -v`
Expected: FAIL — `AttributeError` on `aws_cli.HELPER_FILENAME` / content mismatches (old EcsContainer body)

- [ ] **Step 3: Rewrite the materializer**

Replace the ENTIRE content of `apps/backend/core/services/credentials/materializers/aws_cli.py` with:

```python
"""Materializer for the AWS STS rail: writes the owner's ``.aws`` bundle so the
baked ``aws`` CLI pulls short-lived connector credentials on demand.

Three files under ``{EFS_MOUNT_PATH}/{owner}/.aws/`` (in-container:
``/home/node/.openclaw/.aws/`` — the per-user EFS access point roots
``/users/{owner}`` at ``/home/node/.openclaw``, and the task env
``AWS_CONFIG_FILE`` already points at ``config`` there):

* ``config`` — ``[profile isol8-readonly]`` whose ``credential_process`` runs
  the helper below. The profile is **named**, never ``[default]``: a default
  profile would be picked up by the in-process Bedrock SDK's default chain.
  It carries NO ``role_arn``/``credential_source``/``external_id`` — a profile
  mixing ``role_arn`` with other credential sources flips botocore into
  assume-role mode, and the ExternalId must never touch EFS.
* ``isol8-creds.mjs`` — zero-dependency node ESM helper the CLI executes once
  per invocation: serves the cached credential while >10 min validity remains,
  else fetches from the backend credentials endpoint with the bearer token and
  rewrites the cache. ``/usr/bin/env node`` keeps the command PATH-robust.
* ``token`` — ``{"owner_id", "token"}`` bearer credential (0600). Server-side
  only its SHA-256 hash is retained, so reading Secrets Manager never yields a
  usable bearer.

The exec-env sanitizer strips every env credential channel from agent execs;
a config-file ``credential_process`` is the delivery mechanism that survives.
The files are agent-writable by design — the security boundary is the token +
backend validation, not file integrity (tampering is self-harm only)."""

from __future__ import annotations

import json

from core.config import settings
from core.services.credentials.materializers.base import atomic_write, owner_efs_root

AWS_READONLY_PROFILE = "isol8-readonly"
HELPER_FILENAME = "isol8-creds.mjs"
TOKEN_FILENAME = "token"
CACHE_FILENAME = "cache.json"

# In-container absolute path of the .aws dir (see module docstring).
_CONTAINER_AWS_DIR = "/home/node/.openclaw/.aws"

_HELPER_TEMPLATE = r"""#!/usr/bin/env node
// Isol8 AWS credential_process helper — materialized by the Isol8 backend.
// Prints credential_process JSON: cache while fresh, else fetch from the
// backend with the sibling bearer token. The token is the security boundary.
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const API_BASE = "__API_BASE__";
const DIR = dirname(fileURLToPath(import.meta.url));
const CACHE = join(DIR, "cache.json");
const REFRESH_MARGIN_MS = 10 * 60 * 1000;

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}
function msLeft(creds) {
  return creds && creds.Expiration ? new Date(creds.Expiration).getTime() - Date.now() : -1;
}
function emit(creds) {
  process.stdout.write(JSON.stringify(creds));
}

const cached = readJson(CACHE);
if (msLeft(cached) > REFRESH_MARGIN_MS) {
  emit(cached);
  process.exit(0);
}

const tok = readJson(join(DIR, "token"));
if (!tok || !tok.token) {
  console.error("isol8: AWS connection token missing — reconnect AWS in Isol8 settings");
  process.exit(1);
}

let creds = null;
try {
  const resp = await fetch(`${API_BASE}/api/v1/connections/aws/credentials`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok.token}` },
    body: JSON.stringify({ owner_id: tok.owner_id }),
    signal: AbortSignal.timeout(10_000),
  });
  if (resp.ok) {
    creds = await resp.json();
  } else {
    console.error(`isol8: credential service returned ${resp.status}`);
  }
} catch (err) {
  console.error(`isol8: credential service unreachable (${(err && err.name) || err})`);
}

if (!creds) {
  if (msLeft(cached) > 0) {
    emit(cached); // stale-but-valid cache beats failing the CLI call
    process.exit(0);
  }
  console.error("isol8: no valid AWS credentials — Isol8 credential service unavailable");
  process.exit(1);
}

try {
  writeFileSync(CACHE, JSON.stringify(creds), { mode: 0o600 });
} catch {
  // Cache write failure is non-fatal — creds still go to stdout.
}
emit(creds);
"""


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    aws_dir = owner_efs_root(owner_id) / ".aws"
    config = (
        f"[profile {AWS_READONLY_PROFILE}]\n"
        f"region = {credential['region']}\n"
        f"credential_process = /usr/bin/env node {_CONTAINER_AWS_DIR}/{HELPER_FILENAME}\n"
    )
    helper = _HELPER_TEMPLATE.replace("__API_BASE__", settings.PUBLIC_API_BASE)
    token = json.dumps({"owner_id": owner_id, "token": credential["cli_token"]})
    atomic_write(aws_dir / "config", config.encode())
    atomic_write(aws_dir / HELPER_FILENAME, helper.encode(), mode=0o700)
    atomic_write(aws_dir / TOKEN_FILENAME, token.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    aws_dir = owner_efs_root(owner_id) / ".aws"
    for name in ("config", HELPER_FILENAME, TOKEN_FILENAME, CACHE_FILENAME):
        (aws_dir / name).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `cd apps/backend && uv run pytest tests/credentials/test_materializer_aws_cli.py -v`
Expected: ALL PASS (node tests skip cleanly if node absent locally; they run in CI — ubuntu runners ship node)

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/credentials/materializers/aws_cli.py apps/backend/tests/credentials/test_materializer_aws_cli.py
git commit -m "feat(aws): materialize credential_process bundle (config + node helper + bearer token)"
```

---

### Task 3: Credentials fetch endpoint + token validation

**Files:**
- Modify: `apps/backend/routers/connections.py`
- Test: `apps/backend/tests/unit/routers/test_connections.py`

**Interfaces:**
- Consumes: `integration_secret_store.get`, `integrations_repo.get`, `run_in_thread`, `put_metric` (add import `from core.observability.metrics import put_metric` — check the exact import path other routers use: `routers/billing.py` imports it; mirror that import line).
- Produces: `POST /api/v1/connections/aws/credentials` — body `{"owner_id": str}`, header `Authorization: Bearer <plaintext token>`, returns credential_process JSON `{"Version":1,"AccessKeyId","SecretAccessKey","SessionToken","Expiration"}`. 401 `missing_or_malformed_authorization` / `invalid_token`; 429 rate-limited; 502 `credential_vend_failed`. Also module helper `_hash_cli_token(token: str) -> str` that Task 4 reuses when minting.

- [ ] **Step 1: Write the failing tests**

Add a new test class at the END of `apps/backend/tests/unit/routers/test_connections.py`:

```python
class TestAwsCredentialsEndpoint:
    """POST /api/v1/connections/aws/credentials — the container helper's
    credential_process endpoint. NO Clerk auth: bearer token minted at
    aws-complete, validated against the SHA-256 hash in the SM credential."""

    _TOKEN = "tok_plain_abc"
    # sha256("tok_plain_abc") — precomputed so tests don't re-derive via the
    # same helper they're testing.
    _TOKEN_HASH = "18197ef8f0b27b7cbf4f3acfa60448a622594f7a20279c4fa6a0cfded6a605cc"

    def _stored(self):
        return {
            "role_arn": "arn:aws:iam::123456789012:role/IsolReadOnly",
            "external_id": "ext_abc",
            "region": "us-east-1",
            "cli_token_hash": self._TOKEN_HASH,
        }

    def _sts_response(self):
        from datetime import datetime, timezone

        return {
            "Credentials": {
                "AccessKeyId": "ASIATEST",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc),
            }
        }

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_happy_path_returns_credential_process_json(
        self, mock_store, mock_repo, mock_sts, async_client
    ):
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.return_value = self._sts_response()

        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["Version"] == 1
        assert body["AccessKeyId"] == "ASIATEST"
        assert body["SecretAccessKey"] == "secret"
        assert body["SessionToken"] == "session"
        assert body["Expiration"].startswith("2099-01-01T00:00:00")

        kwargs = mock_sts.return_value.assume_role.call_args.kwargs
        assert kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/IsolReadOnly"
        assert kwargs["ExternalId"] == "ext_abc"
        assert kwargs["DurationSeconds"] == 3600
        assert kwargs["RoleSessionName"].startswith("isol8-")

    @pytest.mark.asyncio
    async def test_missing_bearer_is_401(self, async_client):
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials", json={"owner_id": "org_x"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "missing_or_malformed_authorization"

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    async def test_wrong_token_is_401(self, mock_store, async_client):
        mock_store.get = AsyncMock(return_value=self._stored())
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid_token"

    @pytest.mark.asyncio
    @patch("routers.connections.integration_secret_store")
    async def test_no_connection_record_is_401(self, mock_store, async_client):
        mock_store.get = AsyncMock(return_value=None)
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_disconnected_status_is_401(self, mock_store, mock_repo, async_client):
        # Revocation: SM secret may briefly outlive the status row (or vice
        # versa); either missing/non-connected side must 401.
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value=None)
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_sts_failure_is_502(self, mock_store, mock_repo, mock_sts, async_client):
        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.side_effect = RuntimeError("boom")
        resp = await async_client.post(
            "/api/v1/connections/aws/credentials",
            json={"owner_id": "org_x"},
            headers={"Authorization": f"Bearer {self._TOKEN}"},
        )
        assert resp.status_code == 502

    @pytest.mark.asyncio
    @patch("routers.connections._sts_client")
    @patch("routers.connections.integrations_repo")
    @patch("routers.connections.integration_secret_store")
    async def test_rate_limited_after_burst(self, mock_store, mock_repo, mock_sts, async_client):
        from routers import connections as conn_mod

        mock_store.get = AsyncMock(return_value=self._stored())
        mock_repo.get = AsyncMock(return_value={"status": "connected"})
        mock_sts.return_value.assume_role.return_value = self._sts_response()
        conn_mod._aws_cli_rate_buckets.clear()
        try:
            for _ in range(conn_mod._AWS_CLI_RATE_LIMIT_PER_MINUTE):
                ok = await async_client.post(
                    "/api/v1/connections/aws/credentials",
                    json={"owner_id": "org_rate"},
                    headers={"Authorization": f"Bearer {self._TOKEN}"},
                )
                assert ok.status_code == 200
            over = await async_client.post(
                "/api/v1/connections/aws/credentials",
                json={"owner_id": "org_rate"},
                headers={"Authorization": f"Bearer {self._TOKEN}"},
            )
            assert over.status_code == 429
            assert over.headers["Retry-After"] == "60"
        finally:
            conn_mod._aws_cli_rate_buckets.clear()
```

Note: verify the `_TOKEN_HASH` constant by running `python3 -c "import hashlib; print(hashlib.sha256(b'tok_plain_abc').hexdigest())"` and paste the actual output if it differs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py::TestAwsCredentialsEndpoint -v`
Expected: FAIL — 404/405 (route absent) and `AttributeError` on `_sts_client`

- [ ] **Step 3: Implement the endpoint**

In `apps/backend/routers/connections.py`:

(a) extend imports at the top of the file:

```python
import hashlib
import hmac
import time
from collections import defaultdict, deque

import boto3
from fastapi import APIRouter, Depends, HTTPException, Request, Response
```

(merge with the existing `fastapi` import line rather than duplicating it), and add alongside the other `core` imports:

```python
from core.observability.metrics import put_metric
```

(match the exact import path used in `routers/billing.py`).

(b) add near the other module-level constants (below `_REGION_RE`):

```python
# --- container credential_process endpoint machinery -------------------------
# The helper on the owner's EFS calls POST /aws/credentials with a per-owner
# bearer token (minted at aws-complete; only its SHA-256 hash is retained in
# the SM credential). Legitimate use is ~1 fetch per 50 minutes of active use,
# so the per-owner limit is generous. In-process sliding window, same pattern
# and caveats as routers/hooks_proxy.py (per-task buckets; fine at
# desiredCount=1).
_AWS_CLI_RATE_LIMIT_PER_MINUTE = 10
_aws_cli_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def _check_aws_cli_rate_limit(owner_id: str) -> bool:
    now = time.monotonic()
    cutoff = now - 60.0
    q = _aws_cli_rate_buckets[owner_id]
    while q and q[0] < cutoff:
        q.popleft()
    if len(q) >= _AWS_CLI_RATE_LIMIT_PER_MINUTE:
        return False
    q.append(now)
    return True


def _hash_cli_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sts_client():
    return boto3.client("sts", region_name=settings.AWS_REGION)
```

(c) add the body model next to `AwsCompleteBody`:

```python
class AwsCredentialsBody(BaseModel):
    owner_id: str
```

(d) add the endpoint BEFORE the `/{provider}`-parameterized routes (path shape `/aws/credentials` cannot collide with single-segment `/{provider}` routes, but keeping literal routes first is the file's convention):

```python
@router.post("/aws/credentials")
async def aws_credentials(body: AwsCredentialsBody, request: Request):
    """credential_process endpoint for the container helper. NO Clerk auth —
    the caller is the owner's container; identity is the per-owner bearer
    token minted at aws-complete, compared in constant time against the
    SHA-256 hash retained in the SM credential. The ExternalId never leaves
    this process. Every fetch is metered + logged (the audit trail the old
    EcsContainer design could not offer)."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_or_malformed_authorization")
    presented = auth_header[len("Bearer ") :]

    rec = await integration_secret_store.get(body.owner_id, "aws")
    expected_hash = (rec or {}).get("cli_token_hash") or ""
    if not expected_hash or not hmac.compare_digest(_hash_cli_token(presented), expected_hash):
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "unauthorized"})
        raise HTTPException(status_code=401, detail="invalid_token")

    status_row = await integrations_repo.get(body.owner_id, "aws")
    if not status_row or status_row.get("status") != "connected":
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "unauthorized"})
        raise HTTPException(status_code=401, detail="invalid_token")

    # Rate limit AFTER auth so unauthenticated spray can't fill the bucket.
    if not _check_aws_cli_rate_limit(body.owner_id):
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "rate_limited"})
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content=b'{"detail":"rate_limited"}',
            media_type="application/json",
        )

    try:
        # RoleSessionName charset is [\w+=,.@-]; owner ids (org_/user_ + base62)
        # fit. Truncate to the 64-char API limit.
        session_name = f"isol8-{body.owner_id}"[:64]
        resp = await run_in_thread(
            _sts_client().assume_role,
            RoleArn=rec["role_arn"],
            RoleSessionName=session_name,
            ExternalId=rec["external_id"],
            DurationSeconds=3600,
        )
    except Exception:
        logger.exception("aws credential vend failed for owner=%s", body.owner_id)
        put_metric("connections.aws_credentials.fetch", dimensions={"result": "sts_error"})
        raise HTTPException(status_code=502, detail="credential_vend_failed")

    creds = resp["Credentials"]
    put_metric("connections.aws_credentials.fetch", dimensions={"result": "ok"})
    logger.info("aws credentials vended", extra={"owner_id": body.owner_id})
    return {
        "Version": 1,
        "AccessKeyId": creds["AccessKeyId"],
        "SecretAccessKey": creds["SecretAccessKey"],
        "SessionToken": creds["SessionToken"],
        "Expiration": creds["Expiration"].isoformat(),
    }
```

Note: `put_metric` refuses `owner_id` as a dimension (high-cardinality denylist) — that's why owner goes in the log `extra`, and `result` is the only dimension.

- [ ] **Step 4: Run tests to verify pass**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py::TestAwsCredentialsEndpoint -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/tests/unit/routers/test_connections.py
git commit -m "feat(aws): bearer-token credential_process endpoint vending connector creds"
```

---

### Task 4: Rewire connect flow — mint token, root trust, drop owner-aws-sts calls

**Files:**
- Modify: `apps/backend/routers/connections.py` (aws_setup, aws_complete, `_self_account_role_is_pinned`, disconnect)
- Modify: `apps/backend/core/services/credentials/aws_cfn.py` (delete `owner_task_role_arn`)
- Test: `apps/backend/tests/unit/routers/test_connections.py`, `apps/backend/tests/credentials/test_aws_cfn.py`

**Interfaces:**
- Consumes: `aws_cfn.platform_trust_principal_arn()` (Task 1), `_hash_cli_token` (Task 3), materializer contract `credential["cli_token"]` (Task 2), `secrets` stdlib (add `import secrets` to connections.py imports).
- Produces: SM credential shape becomes `{role_arn, external_id, region, cli_token_hash}`; `apply_connection` is called with that dict PLUS `cli_token` (plaintext, never stored). `aws_cfn.owner_task_role_arn` no longer exists.

- [ ] **Step 1: Update the failing tests first**

In `apps/backend/tests/unit/routers/test_connections.py`:

1. **TestAwsSetup** — in every test asserting the response, the expected `principal_arn` becomes `"arn:aws:iam::877352799272:root"`. If the tests patch `routers.connections.aws_cfn` as a mock, instead set `mock_cfn.platform_trust_principal_arn.return_value = "arn:aws:iam::877352799272:root"` and drop any `owner_task_role_arn` stubs.
2. **TestAwsComplete happy path** (`test_aws_complete_happy_path_grants_materializes_marks_connected`) — replace with:

```python
    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.apply_connection")
    @patch("routers.connections.integration_secret_store")
    @patch("routers.connections.integrations_repo")
    async def test_aws_complete_happy_path_mints_token_materializes_marks_connected(
        self, mock_repo, mock_store, mock_apply, mock_aip, async_client
    ):
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_store.put = AsyncMock()
        mock_store.delete = AsyncMock()
        mock_apply.apply_connection = AsyncMock()
        mock_repo.put_status = AsyncMock()

        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": self._GOOD_ARN, "region": "us-west-2"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "connected", "provider": "aws"}

        stored = mock_store.put.call_args.args[2]
        assert stored["role_arn"] == self._GOOD_ARN
        assert stored["external_id"] == "ext_abc"
        assert stored["region"] == "us-west-2"
        # Only the HASH is retained server-side; plaintext never reaches SM.
        assert "cli_token_hash" in stored and len(stored["cli_token_hash"]) == 64
        assert "cli_token" not in stored

        applied = mock_apply.apply_connection.call_args.args[2]
        # The materializer gets the stored dict PLUS the plaintext token.
        assert applied["cli_token_hash"] == stored["cli_token_hash"]
        import hashlib as _h

        assert _h.sha256(applied["cli_token"].encode()).hexdigest() == stored["cli_token_hash"]

        mock_repo.put_status.assert_awaited_once_with("user_test_123", "aws", "connected")
        # The per-owner IAM grant machinery is retired: no put/delete calls ever.
        mock_aip.put_owner_aws_sts_policy.assert_not_called()
        mock_aip.delete_owner_aws_sts_policy.assert_not_called()
```

3. **Rollback test** — remove the `delete_owner_aws_sts_policy` assertion; keep asserting `_remove_materialized_artifacts` runs and the pending `{external_id}` record is re-PUT.
4. **Trust-pin gate tests** (the our-account ARN tests) — wherever a trust doc fixture pins `aws_cfn.owner_task_role_arn(...)` / a per-owner role ARN, pin `"arn:aws:iam::877352799272:root"` instead, and stub `mock_cfn.platform_trust_principal_arn.return_value` accordingly. A trust doc pinning the OLD per-owner principal must now FAIL the gate (400) — add that as an explicit test if not already produced by the fixture change:

```python
    @pytest.mark.asyncio
    @patch("routers.connections.bedrock_aip")
    @patch("routers.connections.integration_secret_store")
    async def test_our_account_arn_with_per_owner_principal_trust_now_rejected(
        self, mock_store, mock_aip, async_client
    ):
        # Pre-migration trust docs (pinning the per-owner container role) must
        # no longer pass: the platform principal is account root.
        mock_aip.OUR_ACCOUNT_ID = "877352799272"
        mock_store.get = AsyncMock(return_value={"external_id": "ext_abc"})
        mock_aip._iam_client.return_value.get_role.return_value = {
            "Role": {
                "AssumeRolePolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "AWS": "arn:aws:iam::877352799272:role/isol8-prod-owner-bedrock-user_test_123"
                            },
                            "Condition": {"StringEquals": {"sts:ExternalId": "ext_abc"}},
                        }
                    ]
                }
            }
        }
        resp = await async_client.post(
            "/api/v1/connections/aws/aws-complete",
            json={"role_arn": "arn:aws:iam::877352799272:role/isol8-connector"},
        )
        assert resp.status_code == 400
```

5. **Disconnect tests** — remove assertions that `mock_aip.delete_owner_aws_sts_policy` is awaited; assert instead that `mock_store.delete` and `mock_repo.delete` are awaited (revocation = hash gone → endpoint 401s).

In `apps/backend/tests/credentials/test_aws_cfn.py`: DELETE `test_owner_task_role_arn_reuses_bedrock_aip_name`.

- [ ] **Step 2: Run to verify the updated tests fail**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py -k "aws" -v`
Expected: the updated tests FAIL against current code (old grant call still present, principal still per-owner)

- [ ] **Step 3: Implement the rewiring**

In `apps/backend/routers/connections.py`:

(a) `import secrets` at the top.

(b) **aws_setup** — replace `principal_arn = aws_cfn.owner_task_role_arn(owner_id)` with:

```python
    principal_arn = aws_cfn.platform_trust_principal_arn()
```

(c) **`_self_account_role_is_pinned`** — replace the `expected_principal` line and update the docstring:

```python
async def _self_account_role_is_pinned(role_arn: str, owner_id: str, external_id: str) -> bool:
    """True iff a role IN OUR account is a legitimate connector for THIS owner:
    its trust policy allows the platform trust principal (our account root) AND
    requires the owner's ExternalId. Only we can create such a role, and our
    infra roles (which trust service principals, never root+ExternalId) never
    satisfy it — so this blocks connecting an arbitrary our-account role
    (account-takeover) without an owner-id allowlist. Any failure (GetRole
    error, shape mismatch) -> False."""
    role_name = role_arn.split(":role/", 1)[1]
    try:
        resp = await run_in_thread(bedrock_aip._iam_client().get_role, RoleName=role_name)
        trust = resp["Role"]["AssumeRolePolicyDocument"]  # boto3 returns a decoded dict
        expected_principal = aws_cfn.platform_trust_principal_arn()
```

(rest of the function body unchanged).

(d) **aws_complete** — replace the credential-build + grant/apply block (currently lines ~232-245 of the extract, `credential = {...}` through `put_status`) with:

```python
    # Mint the container's bearer token for the credential_process endpoint.
    # Only its hash is retained (SM readers must never gain a usable bearer);
    # the plaintext rides the materialize call onto the owner's EFS and
    # nowhere else.
    cli_token = secrets.token_urlsafe(32)
    credential = {
        "role_arn": role_arn,
        "external_id": external_id,
        "region": body.region or p.aws_sts.default_region,
        "cli_token_hash": _hash_cli_token(cli_token),
    }
    # Overwrite the pending {external_id} record with the full credential
    # (retained for the vend endpoint + reconcile). Then materialize -> mark
    # connected, with the status row written ONLY after a clean apply so the
    # integration never reports a false "connected". No IAM grant step: the
    # backend's own role performs the AssumeRole at vend time.
    await integration_secret_store.put(owner_id, provider, credential)
    try:
        await apply_connection.apply_connection(
            owner_id, provider, {**credential, "cli_token": cli_token}
        )
        await integrations_repo.put_status(owner_id, provider, "connected")
```

and in the `except` rollback block, DELETE the `bedrock_aip.delete_owner_aws_sts_policy` try/except (keep `_remove_materialized_artifacts` and the re-PUT of `{"external_id": external_id}`). Update the endpoint docstring's "We grant the per-owner task role sts:AssumeRole…" sentence to: `We mint the container's credential-fetch bearer token, materialize the ~/.aws credential_process bundle, and mark the integration connected.` and trim the confused-deputy paragraph's mention of the inline grant to refer to the vend endpoint's exact-ARN AssumeRole instead.

(e) **disconnect** — DELETE the aws_sts kill-switch block (the comment + `if p.acquisition == "aws_sts": await bedrock_aip.delete_owner_aws_sts_policy(owner_id)`) and replace with:

```python
    # aws_sts revocation is server-side: deleting the secret below drops the
    # cli_token_hash, so the container's next credential fetch 401s. Already-
    # vended sessions expire within 1h (DurationSeconds cap).
```

(f) In `apps/backend/core/services/credentials/aws_cfn.py`: DELETE `owner_task_role_arn` (its docstring included) and update the module docstring's trust sentence to: `The trusted principal is our account root (platform_trust_principal_arn) with the per-owner ExternalId condition as the lock.`

- [ ] **Step 4: Run the full aws test surface**

Run: `cd apps/backend && uv run pytest tests/unit/routers/test_connections.py tests/credentials/ -v`
Expected: ALL PASS. If `grep -rn "owner_task_role_arn" apps/backend --include="*.py"` returns any hit, fix it — there must be zero.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/routers/connections.py apps/backend/core/services/credentials/aws_cfn.py apps/backend/tests/unit/routers/test_connections.py apps/backend/tests/credentials/test_aws_cfn.py
git commit -m "feat(aws): root-trust connect flow — mint fetch token, retire per-owner grant calls"
```

---

### Task 5: Retire the owner-aws-sts machinery in bedrock_aip

**Files:**
- Modify: `apps/backend/core/billing/bedrock_aip.py`
- Delete: `apps/backend/tests/credentials/test_aws_sts_grant.py`

**Interfaces:**
- Consumes: nothing new. Preconditions: Task 4 removed the last production callers (verify: `grep -rn "owner_aws_sts\|_build_owner_aws_sts_doc" apps/backend --include="*.py"` shows only bedrock_aip.py + the test file being deleted).
- Produces: `bedrock_aip` keeps `OUR_ACCOUNT_ID` and `CONNECTOR_ROLE_NAME` (both still consumed by aws_cfn/connections/service-stack docs); loses the three aws-sts functions.

- [ ] **Step 1: Delete the test file**

```bash
git rm apps/backend/tests/credentials/test_aws_sts_grant.py
```

- [ ] **Step 2: Delete the functions**

In `apps/backend/core/billing/bedrock_aip.py` delete, entirely: `_build_owner_aws_sts_doc` (lines ~588-629), `put_owner_aws_sts_policy` (~632-663), `delete_owner_aws_sts_policy` (~666-683). Update the `OUR_ACCOUNT_ID` comment block (lines 38-45): drop the sentence referencing `_build_owner_aws_sts_doc` and the owner-aws-sts Deny; keep the constant with a comment like:

```python
# Our own AWS account (dev + prod share account 877352799272). Named once here
# so aws_cfn / connections can reference it without an STS call; runtime ARNs
# elsewhere in the module use _caller_account_id() instead.
OUR_ACCOUNT_ID: str = "877352799272"
```

Keep `CONNECTOR_ROLE_NAME` untouched.

- [ ] **Step 3: Verify nothing references the removed names**

Run: `grep -rn "owner_aws_sts\|_build_owner_aws_sts_doc" apps/backend --include="*.py"`
Expected: no output.

- [ ] **Step 4: Run the backend suite for this area**

Run: `cd apps/backend && uv run pytest tests/credentials/ tests/unit/routers/test_connections.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add -A apps/backend
git commit -m "refactor(aws): retire per-owner owner-aws-sts grant machinery"
```

---

### Task 6: CDK — unconditional boundary Deny, drop the STS Allow ceiling

**Files:**
- Modify: `apps/infra/lib/stacks/container-stack.ts:281-364`
- Test: `apps/infra/test/container-stack.test.ts`

**Interfaces:**
- Consumes: nothing from other tasks (independent; ship in the same PR).
- Produces: `OwnerBedrockBoundary` without `BoundaryStsAssumeRole` and with an unconditional `BoundaryDenyAssumeIntoOurAccount`. `apps/infra/lib/app.ts` untouched (its `PLATFORM_OPS_OWNER_ID_*` plumbing feeds service-stack's backend env var).

- [ ] **Step 1: Write the failing test**

Append to `apps/infra/test/container-stack.test.ts` (inside the same describe block as the existing boundary tests):

```typescript
  test("boundary denies assume-into-our-account unconditionally and grants no sts:AssumeRole", () => {
    // The per-owner container role has no AWS-rail job anymore: the backend
    // vends connector credentials itself. The Deny must be unconditional
    // (no founder carve-out) and the broad AssumeRole ceiling must be gone.
    const policies = template.findResources("AWS::IAM::ManagedPolicy");
    const boundary = Object.values(policies).find(
      (p: any) => p.Properties.ManagedPolicyName === "isol8-dev-owner-bedrock-boundary",
    ) as any;
    expect(boundary).toBeDefined();

    const stmts = boundary.Properties.PolicyDocument.Statement as any[];
    const deny = stmts.find((s) => s.Effect === "Deny");
    expect(deny).toBeDefined();
    expect(JSON.stringify(deny.Action)).toContain("sts:AssumeRole");
    expect(deny.Condition).toBeUndefined();

    const allowAssume = stmts.find(
      (s) => s.Effect === "Allow" && JSON.stringify(s.Action).includes("sts:AssumeRole"),
    );
    expect(allowAssume).toBeUndefined();
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/infra && npx jest test/container-stack.test.ts -t "unconditionally"`
Expected: FAIL — `allowAssume` is defined (BoundaryStsAssumeRole present). Note: the Condition assertion may pass already when `PLATFORM_OPS_OWNER_ID_*` env is unset locally (the carve is env-gated) — the Allow assertion is the reliable failure.

- [ ] **Step 3: Implement**

In `apps/infra/lib/stacks/container-stack.ts`:

1. DELETE the `selfAccountOwnerId` / `selfAccountRoleArn` block (lines ~281-295) including its carve-out comment paragraph.
2. DELETE the entire `BoundaryStsAssumeRole` statement and its comment (lines ~339-354 of the current file — the `// Scoped STS for the AWS read-only integration…` comment plus the `new iam.PolicyStatement({ sid: "BoundaryStsAssumeRole", … })`). Nothing grants per-owner roles `sts:AssumeRole` anymore, so the ceiling Allow is dead weight.
3. Replace the Deny statement + comment with:

```typescript
        // Defense in depth: never let a per-owner role assume into OUR account,
        // even if a misconfigured inline policy tried to. An explicit Deny
        // always wins. Unconditional: the AWS integration's credentials are
        // vended by the backend, so no per-owner role ever needs an assume.
        new iam.PolicyStatement({
          sid: "BoundaryDenyAssumeIntoOurAccount",
          effect: iam.Effect.DENY,
          actions: ["sts:AssumeRole"],
          resources: [`arn:aws:iam::${this.account}:role/*`],
        }),
```

4. Update the boundary's lead comment (lines ~274-280) to drop the carve-out sentences (keep the "UNION of what it may ever hold" framing, now: scoped Bedrock invoke + EFS + logs + ssm).

- [ ] **Step 4: Run the infra tests**

Run: `cd apps/infra && npx jest test/container-stack.test.ts`
Expected: ALL PASS (the two pre-existing boundary tests never asserted the Deny/Allow being removed)

- [ ] **Step 5: Commit**

```bash
git add apps/infra/lib/stacks/container-stack.ts apps/infra/test/container-stack.test.ts
git commit -m "feat(infra): unconditional boundary deny — per-owner roles lose all sts:AssumeRole"
```

---

### Task 7: Connector naming ("readonly" purge) + docs drift fixes

**Files:**
- Rename: `apps/infra/aws/aws-readonly-connector.yaml` → `apps/infra/aws/aws-connector.yaml` (`git mv`; prose updates inside — logical ID and RoleName untouched)
- Modify: `apps/infra/lib/stacks/service-stack.ts:864-879` (`CFN_TEMPLATE_KEY` + comment)
- Modify: `apps/infra/aws/README.md`
- Modify: `apps/backend/core/services/credentials/materializers/aws_cli.py` + `apps/backend/tests/credentials/test_materializer_aws_cli.py` (profile → `isol8-connector`)
- Modify: `apps/backend/core/containers/config.py:666` (comment)
- Modify: all 6 plugin files under `apps/infra/openclaw/plugins/aws-exec-profile/`
- Modify: `apps/backend/core/services/credentials/providers.yaml` (aws block comment), `apps/backend/core/services/credentials/registry.py` (AwsStsSpec docstring)

**Interfaces:**
- Produces: profile name `isol8-connector` everywhere; materializer constant renamed `AWS_READONLY_PROFILE` → `AWS_CONNECTOR_PROFILE`; plugin exports renamed `AWS_READONLY_PROFILE`/`AWS_READONLY_PROFILE_HEADER` → `AWS_CONNECTOR_PROFILE`/`AWS_CONNECTOR_PROFILE_HEADER`. The materializer and plugin MUST agree on `[profile isol8-connector]` — they ship in the same PR; the image rebuild + container update ordering is handled in Task 8.

- [ ] **Step 1: Profile rename — failing tests first (backend)**

In `apps/backend/tests/credentials/test_materializer_aws_cli.py`:
- In `test_config_uses_credential_process_and_no_secrets`, change the expected body's first line to `"[profile isol8-connector]\n"`.
- Everywhere else the file references `aws_cli.AWS_READONLY_PROFILE`, rename to `aws_cli.AWS_CONNECTOR_PROFILE`.
- In `test_named_profile_not_default_so_bedrock_chain_is_unaffected`, the startswith assertion stays constant-derived (now via `AWS_CONNECTOR_PROFILE`).

Run: `cd apps/backend && uv run pytest tests/credentials/test_materializer_aws_cli.py -v`
Expected: FAIL (config body still `isol8-readonly`, constant missing)

- [ ] **Step 2: Profile rename — implementation (backend + plugin, lockstep)**

Backend `apps/backend/core/services/credentials/materializers/aws_cli.py`:
- `AWS_READONLY_PROFILE = "isol8-readonly"` → `AWS_CONNECTOR_PROFILE = "isol8-connector"` (update the f-string in `write()` and both docstring mentions of `[profile isol8-readonly]` → `[profile isol8-connector]`).

Backend `apps/backend/core/containers/config.py:666`: comment text `AWS_PROFILE=isol8-readonly` → `AWS_PROFILE=isol8-connector`.

Plugin (naming only — hook wiring and resolver LOGIC untouched; keep the dist/index.ts SYNC NOTE discipline):
- `dist/resolve.js`: `export const AWS_READONLY_PROFILE = "isol8-readonly";` → `export const AWS_CONNECTOR_PROFILE = "isol8-connector";`; `AWS_READONLY_PROFILE_HEADER` → `AWS_CONNECTOR_PROFILE_HEADER` (and its template-literal reference); the regex builder and `return { AWS_PROFILE: ... }` reference the renamed constant; update the comments mentioning `isol8-readonly` / "the materializer's AWS_READONLY_PROFILE constant" to the new names.
- `dist/index.js:30` and `index.ts:39` descriptions: `"Injects AWS_PROFILE=isol8-readonly into the agent's exec environment only."` → `isol8-connector`; `index.ts:29-33` comment "owner's read-only profile" → "owner's connector profile".
- `openclaw.plugin.json:4` and `package.json:5` descriptions: same `isol8-readonly` → `isol8-connector` substitution (and "read-only AWS profile" → "connector AWS profile").
- `index.test.mjs`: import `AWS_CONNECTOR_PROFILE_HEADER`; expected `{ AWS_PROFILE: "isol8-connector" }`; test names "readonly profile" → "connector profile".

Run plugin tests: `cd apps/infra/openclaw/plugins/aws-exec-profile && node --test index.test.mjs`
Expected: PASS
Run backend tests: `cd apps/backend && uv run pytest tests/credentials/test_materializer_aws_cli.py -v`
Expected: ALL PASS (node e2e tests included — they are profile-name-agnostic)

- [ ] **Step 3: Template file rename + CDK key**

```bash
git mv apps/infra/aws/aws-readonly-connector.yaml apps/infra/aws/aws-connector.yaml
```

In `apps/infra/lib/stacks/service-stack.ts`: `const CFN_TEMPLATE_KEY = "aws-readonly-connector.yaml";` → `const CFN_TEMPLATE_KEY = "aws-connector.yaml";` and in the comment block above it, `The template (apps/infra/aws/ aws-readonly-connector.yaml)` → `The template (apps/infra/aws/aws-connector.yaml)` and "Hosts the AWS read-only connector CloudFormation template" → "Hosts the AWS connector CloudFormation template". Check the BucketDeployment construct below it for any literal filename reference and update it the same way.

Add above the `Isol8ReadOnlyRole:` logical ID in `apps/infra/aws/aws-connector.yaml`:
```yaml
  # Logical ID predates the connector rename. Changing a logical ID REPLACES
  # the resource in any already-deployed stack updated with this template —
  # which would delete + recreate the role and break its trust mid-flight.
  # It stays. (RoleName below is what anyone actually sees.)
```

Run: `cd apps/infra && npx jest` — if any test asserts the old filename/URL, update it to `aws-connector.yaml`.
Then: `grep -rn "aws-readonly-connector" apps/ .github/ --include="*.*" | grep -v node_modules` — expected: no output.

- [ ] **Step 4: Template prose (in the renamed file)**

Apply exactly these text changes (nothing else — `RoleName: isol8-connector`, the `Isol8ReadOnlyRole` logical ID, `MaxSessionDuration`, AllowedValues, and the trust JSON structure all stay):

- Stack `Description` →
  ```yaml
  Description: >-
    Isol8 connector — grants Isol8 scoped AssumeRole access to this account
    at the access tier you selected, gated by an STS ExternalId.
  ```
- `TrustedPrincipalArn` parameter `Description` →
  ```yaml
    Description: >-
      The Isol8 platform principal allowed to assume this role — Isol8's AWS
      account root (arn:aws:iam::877352799272:root). Isol8's backend performs
      the AssumeRole; the ExternalId condition scopes it to this connection.
  ```
- Role `Description` →
  ```yaml
      Description: >-
        Connector role assumed by Isol8's backend on behalf of this account's
        Isol8 owner. Trust is pinned to the Isol8 platform principal and gated
        by an STS ExternalId. Access tier = the attached managed policy.
  ```
- Output `Description` →
  ```yaml
    Description: >-
      ARN of the created connector role — paste this back into Isol8 to
      complete the AWS connection (aws-complete).
  ```

- [ ] **Step 5: README.md rewrite**

Replace the body of `apps/infra/aws/README.md` with:

```markdown
# Isol8 AWS connector

`aws-connector.yaml` is the CloudFormation template an owner launches
in **their own** AWS account to create a scoped role that Isol8's backend can
assume on their behalf.

- It is launched via the CloudFormation console **quick-create link** that the
  `connections/{provider}/aws-setup` endpoint builds (see
  `apps/backend/core/services/credentials/aws_cfn.py`). The link prefills the
  `ExternalId`, `TrustedPrincipalArn`, and `ManagedPolicyArn` parameters.
- `ManagedPolicyArn` is constrained via `AllowedValues` to exactly the three
  offered scope tiers — `job-function/ViewOnlyAccess`, `ReadOnlyAccess`, and
  `PowerUserAccess` — so an owner can't fat-finger a broader policy
  (e.g. `AdministratorAccess`) onto the role.
- The template is hosted at the public URL configured via `CFN_TEMPLATE_URL`
  (falling back to `aws_sts.cfn_template_url` in
  `apps/backend/core/services/credentials/providers.yaml`).
- **Trust:** the role trusts Isol8's platform principal — our account root
  (`arn:aws:iam::877352799272:root`) — **and** requires the per-connection
  `sts:ExternalId` on every AssumeRole call. Isol8's backend performs the
  assume and vends the short-lived credentials to the owner's container via
  its `credential_process` endpoint; the container itself holds no AWS
  identity.
- Because the role uses a fixed name (`isol8-connector`), launching the stack
  requires acknowledging **CAPABILITY_NAMED_IAM**.

After the stack is created, the owner copies the `RoleArn` stack output back
into Isol8 (`aws-complete`) to finish the connection.
```

- [ ] **Step 6: providers.yaml + registry docstring drift**

In `providers.yaml`, replace the aws block's leading comment with:

```yaml
# Bucket B variant — AWS via STS AssumeRole. The owner runs a one-time
# CloudFormation stack creating a connector role (trust = Isol8 account root +
# per-connection ExternalId) at a chosen tier (ViewOnly / ReadOnly /
# PowerUser). The aws_cli materializer writes a credential_process bundle onto
# EFS; the baked `aws` CLI pulls short-lived connector credentials from the
# backend's /connections/aws/credentials endpoint on demand. refresh_owner:
# none — the CLI-side helper refreshes by re-fetching; nothing is scheduled.
```

In `registry.py`, update the `AwsStsSpec` docstring:

```python
class AwsStsSpec(BaseModel):
    """AWS access via STS AssumeRole. The owner runs a CloudFormation stack
    that creates a connector role Isol8's backend can assume (trust = our
    account root + per-connection ExternalId), capped at the tier the owner
    picked from the three AWS-managed policy ARNs below. The aws_cli
    materializer delivers a credential_process bundle; the container's CLI
    pulls short-lived credentials from the backend on demand."""
```

- [ ] **Step 7: Verify + full check**

Run: `cd apps/backend && uv run pytest tests/credentials/ -v` — ALL PASS.
Run: `grep -rn "isol8-readonly\|AWS_READONLY_PROFILE" apps/ --include="*.py" --include="*.js" --include="*.ts" --include="*.mjs" --include="*.json" --include="*.yaml" | grep -v node_modules` — expected: no output.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(aws): rename connector surface — isol8-connector profile, aws-connector.yaml template"
```

---

### Task 8: Full-suite gate, PR, deploy, migrate, live-validate

**Files:** none created — verification + ops runbook. Execute strictly in order.

- [ ] **Step 1: Full test suites**

```bash
cd apps/backend && uv run pytest tests/ -v          # full backend suite
cd ../infra && npx jest                              # full infra suite
```
Expected: ALL PASS. (Do NOT run the frontend vitest suite — nothing frontend changed; it's RAM-heavy by repo policy.)

- [ ] **Step 2: Push branch + open PR** (needs user approval to push, per repo policy)

```bash
git push -u origin feat/aws-backend-materialized-creds
gh pr create --repo Isol8AI/isol8 --title "feat(aws): pull-based connector credentials via credential_process" --body "<summary + spec link docs/superpowers/specs/2026-07-06-aws-backend-materialized-creds-design.md>"
```
Watch CI (`gh run watch <id> --exit-status`); address Codex-review findings; merge when approved.

- [ ] **Step 3: Watch the deploys (two workflows fire on the merge)**

`deploy.yml` runs on the merge commit (dev then prod, same run): `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`. Confirm `Deploy prodisol8prodservice…: success` (backend image + CFN_TEMPLATE_KEY upload) and the container stack deploy succeeded (boundary revert).
`build-openclaw-image.yml` ALSO fires (the plugin rename touches `apps/infra/openclaw/plugins/**`): watch it, then read the new extended tag from its run summary (format `2026.6.10-isol8.2-<shortsha>`).

- [ ] **Step 4: Promote the new image + update the founder container**

1. Second commit/PR: set BOTH `dev.tag` and `prod.tag` in `openclaw-version.json` to the new extended tag from Step 3 (do not touch `upstream`/`image` fields). Merge; watch its `deploy.yml` run (rolls the per-user openclaw task definition to the new image).
2. Update the founder's container to the new task definition via the **admin dashboard** (`admin.isol8.co` → founder user → container → reprovision/apply update; the admin `container.reprovision` action preserves EFS — only the ECS service/task rolls). Wait until the container is healthy again.
3. Order matters: this must complete BEFORE Step 6's `aws-complete` — the new image's plugin matches `[profile isol8-connector]`, which is what the new backend will write.

- [ ] **Step 5: Preflight the helper runtime on the founder container** (read-only)

```bash
CLUSTER=$(aws ecs list-clusters --profile isol8-admin --query "clusterArns[?contains(@,'prod-container')]|[0]" --output text)
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --profile isol8-admin --query "taskArns[0]" --output text \
  --service-name $(aws ecs list-services --cluster "$CLUSTER" --profile isol8-admin --query "serviceArns[?contains(@,'org_3DBS8G83oPrMm49yN2b70EjoFpN')]|[0]" --output text | awk -F/ '{print $NF}'))
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container openclaw --interactive \
  --command "/usr/bin/env node --version" --profile isol8-admin
```
Expected: a node version prints (≥18). If `/usr/bin/env node` fails, STOP — the `credential_process` line in the materializer must be adjusted to the actual node path before migrating.

- [ ] **Step 6: Migrate the founder connection** (NO stack deletion, NO disconnect — the deployed CFN stack, its role, tier, and ExternalId all survive)

1. Update the live connector stack's trust — parameter-only, in place (`--use-previous-template`; the ExternalId stays the one already baked into both the stack and the SM secret):
   ```bash
   aws cloudformation update-stack --stack-name isol8-connector --use-previous-template \
     --parameters ParameterKey=TrustedPrincipalArn,ParameterValue=arn:aws:iam::877352799272:root \
                  ParameterKey=ExternalId,UsePreviousValue=true \
                  ParameterKey=ManagedPolicyArn,UsePreviousValue=true \
     --capabilities CAPABILITY_NAMED_IAM --profile isol8-admin
   aws cloudformation wait stack-update-complete --stack-name isol8-connector --profile isol8-admin
   ```
2. Clean up the stale per-owner grant (the new code no longer manages it):
   ```bash
   aws iam delete-role-policy --role-name isol8-prod-owner-bedrock-org_3DBS8G83oPrMm49yN2b70EjoFpN \
     --policy-name owner-aws-sts --profile isol8-admin
   ```
3. Re-run `aws-complete` as the founder (mints the bearer token + writes the new `credential_process` bundle; the existing SM record already holds the `external_id`, so no `aws-setup` is needed). Browser console on any signed-in isol8.co page:
   ```javascript
   await fetch('https://api.isol8.co/api/v1/connections/aws/aws-complete', {
     method: 'POST',
     headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + await Clerk.session.getToken()},
     body: JSON.stringify({role_arn: 'arn:aws:iam::877352799272:role/isol8-connector'})
   }).then(r => r.json())
   ```
   Expect `{"status": "connected", "provider": "aws"}` — the trust-pin gate validates root + the existing ExternalId against the live role.

- [ ] **Step 7: Acceptance criteria (from the spec — run from a gateway agent exec, i.e. ask Jarvis)**

1. `aws sts get-caller-identity` → `"Arn": ".../assumed-role/isol8-connector/isol8-org_..."` (NOT the task role).
2. Cache: run `aws sts get-caller-identity` twice; backend logs/metrics show ONE `connections.aws_credentials.fetch result=ok` for the pair.
3. Tier: `aws s3 mb s3://isol8-acceptance-$(date +%s)` succeeds (PowerUser tier) — then delete the bucket. (On read tiers this must fail with AccessDenied.)
4. Revocation: disconnect in UI → ask Jarvis to delete the cache file (`rm /home/node/.openclaw/.aws/cache.json` — disconnect already removed it, so this is a no-op check) → `aws sts get-caller-identity` fails with the helper's `isol8:` error naming the credential service. Reconnect (aws-setup reuses nothing — full flow again incl. stack ExternalId update) or leave disconnected per the founder's preference. **Alternative non-destructive check:** temporarily rename the token file instead of disconnecting.
5. Bedrock unaffected: send Jarvis any normal chat message (inference works); `AWS_PROFILE` absent from the node process env.

- [ ] **Step 8: Close out**

Update memory (`project_aws_readonly_integration_plan`) with the shipped pull design; note in the PR/session that Open Question 3 of the 2026-07-05 spec is resolved by design.

---

## Self-review notes (already applied)

- **Spec coverage:** §1 pull flow → Tasks 2+3+4; §2 files/tamper → Task 2; §3 trust → Tasks 1+4+7 + migration step 5; §4 token rail → Tasks 3+4; §5 retirements → Tasks 5+6; §6 failure modes → helper tests (Task 2) + endpoint tests (Task 3); §7 migration → Task 8; acceptance criteria → Task 8 step 6. Rejected-alternatives/naming sections need no code.
- **Known intentional gaps:** profile rename (`isol8-readonly`) deferred to next image bump per spec; frontend untouched (aws-setup response shape unchanged — only the `principal_arn` value differs); no DDB-backed rate limiter (desiredCount=1; hooks_proxy precedent carries the same caveat).
- **Type consistency:** `credential["cli_token"]` (plaintext, materializer input, Task 2 ⇄ Task 4), `cli_token_hash` (SM field, Task 3 ⇄ Task 4), `platform_trust_principal_arn()` (Task 1 ⇄ Task 4 ⇄ Task 7 prose), `_hash_cli_token` (Task 3 ⇄ Task 4) — names match across tasks.
