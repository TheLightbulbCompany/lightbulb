"""Unit tests for the ChatGPT device-code OAuth orchestration.

Covers OpenAI's actual Codex CLI device-code flow:
  POST /api/accounts/deviceauth/usercode  →  device_auth_id + user_code
  POST /api/accounts/deviceauth/token     →  authorization_code + code_verifier
  POST /oauth/token                       →  access_token + refresh_token
"""

import json
import time
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.services import oauth_service
from core.services.oauth_service import (
    DevicePollPending,
    DevicePollResult,
    get_decrypted_tokens,
    OAuthAlreadyActiveError,
    OAuthBoundElsewhereError,
    OAuthExchangeFailedError,
    poll_device_code,
    request_device_code,
    revoke_user_oauth,
    VERIFICATION_URL,
)


@pytest.fixture
def oauth_table(monkeypatch):
    from core.config import settings as _settings

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-oauth-tokens",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        # core.encryption.get_fernet reads settings.ENCRYPTION_KEY (cached at
        # import time) — patching the env var alone is insufficient.
        monkeypatch.setattr(_settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield


def _ok(json_body: dict, url: str = "https://example.test/x") -> httpx.Response:
    return httpx.Response(200, json=json_body, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_request_device_code_returns_user_facing_fields(oauth_table):
    """Backend POST to /api/accounts/deviceauth/usercode returns the
    user-code that will go on screen + the verification URL we display."""

    async def fake_post(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_abc",
                "user_code": "ABCD-1234",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        result = await request_device_code(user_id="u_1", bound_owner_id="u_1")

    assert result.user_code == "ABCD-1234"
    assert result.verification_uri == VERIFICATION_URL
    assert result.interval == 5
    # Server-side device_auth_id is NOT returned to the caller — kept in DDB.
    assert not hasattr(result, "device_auth_id")


@pytest.mark.asyncio
async def test_poll_pending_returns_pending(oauth_table):
    """While the user hasn't entered the code, OpenAI returns 403/404 —
    we translate to DevicePollPending."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_pending",
                "user_code": "WAIT-0001",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1", bound_owner_id="u_1")

    async def fake_poll(self, url, **kwargs):
        return httpx.Response(403, request=httpx.Request("POST", str(url)))

    with patch.object(httpx.AsyncClient, "post", new=fake_poll):
        result = await poll_device_code(user_id="u_1")
    assert result is DevicePollPending


@pytest.mark.asyncio
async def test_poll_success_persists_encrypted_tokens(oauth_table):
    """Successful poll: deviceauth/token returns authorization_code +
    code_verifier; we exchange those at /oauth/token for the real
    access+refresh tokens, which we Fernet-encrypt into DDB."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_ok",
                "user_code": "OKAY-9999",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1", bound_owner_id="u_1")

    # Two-stage success:
    #   1st call: deviceauth/token → returns authorization_code + verifier
    #   2nd call: /oauth/token      → returns access + refresh tokens
    call_count = {"n": 0}

    async def fake_poll_then_exchange(self, url, **kwargs):
        call_count["n"] += 1
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok(
                {
                    "authorization_code": "auth_code_xyz",
                    "code_verifier": "verifier_xyz",
                    "code_challenge": "challenge_xyz",
                },
                url=url_str,
            )
        if "/oauth/token" in url_str:
            return _ok(
                {
                    "access_token": "eyJ.fake-jwt.access",
                    "refresh_token": "rt_opaque_1",
                    "id_token": "eyJ.id-token.x",
                    "account_id": "chatgpt-account-1",
                    "expires_in": 3600,
                },
                url=url_str,
            )
        raise AssertionError(f"unexpected URL: {url_str}")

    with patch.object(httpx.AsyncClient, "post", new=fake_poll_then_exchange):
        before_ms = int(time.time() * 1000)
        result = await poll_device_code(user_id="u_1")
        after_ms = int(time.time() * 1000)

    assert isinstance(result, DevicePollResult)
    assert result.account_id == "chatgpt-account-1"
    assert call_count["n"] == 2  # poll + exchange

    # Tokens are stored encrypted, so the raw DDB row should NOT contain them
    # in plaintext.
    client = boto3.client("dynamodb", region_name="us-east-1")
    raw_item = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "u_1"}})["Item"]
    raw_payload_b = raw_item["encrypted_tokens"]["B"]
    assert b"eyJ.fake-jwt.access" not in raw_payload_b
    assert b"rt_opaque_1" not in raw_payload_b

    # The access token's real expiry (ms epoch) is persisted so staged
    # credentials aren't treated as already-expired — a consumer that sees
    # no expiry refreshes on first use, needlessly rotating the refresh token.
    from core.services.oauth_service import get_decrypted_tokens

    stored = await get_decrypted_tokens(user_id="u_1")
    assert stored is not None
    assert before_ms + 3600_000 <= stored["expires_at"] <= after_ms + 3600_000


@pytest.mark.asyncio
async def test_poll_propagates_exchange_failure(oauth_table):
    """If /oauth/token rejects the exchange, we surface a clean error."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_fail",
                "user_code": "FAIL-0001",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1", bound_owner_id="u_1")

    async def fake_poll_exchange_400(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok(
                {
                    "authorization_code": "ac",
                    "code_verifier": "v",
                },
                url=url_str,
            )
        return httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url_str))

    with patch.object(httpx.AsyncClient, "post", new=fake_poll_exchange_400):
        with pytest.raises(OAuthExchangeFailedError):
            await poll_device_code(user_id="u_1")


@pytest.mark.asyncio
async def test_poll_legacy_row_raises_clean_error(oauth_table):
    """A pending row from the pre-Codex-CLI flow has `device_code` in
    place of `device_auth_id`. We must NOT KeyError on it — instead
    raise OAuthExchangeFailedError so the router returns 502 and the
    user can click Try Again to restart."""
    # Manually write a legacy-shape pending row (no device_auth_id).
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={
            "user_id": {"S": "u_legacy"},
            "state": {"S": "pending"},
            "device_code": {"S": "old-style-code"},
            "user_code": {"S": "LEGACY-1"},
            "interval": {"N": "5"},
        },
    )
    with pytest.raises(OAuthExchangeFailedError, match="Stale OAuth session"):
        await poll_device_code(user_id="u_legacy")


@pytest.mark.asyncio
async def test_request_device_code_refuses_to_clobber_active_session(oauth_table):
    """Once a user has active tokens, request_device_code refuses to
    overwrite them — caller must revoke first."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_x",
                "user_code": "OK-9",
                "interval": 5,
            },
            url=str(url),
        )

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {
                "access_token": "eyJ.x.y",
                "refresh_token": "rt",
                "id_token": "eyJ.z",
                "account_id": "acc",
            },
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1", bound_owner_id="u_1")
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        await poll_device_code(user_id="u_1")

    # User now has state=active. A second request_device_code must refuse.
    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthAlreadyActiveError):
            await request_device_code(user_id="u_1", bound_owner_id="u_1")


@pytest.mark.asyncio
async def test_revoke_deletes_oauth_row(oauth_table):
    """revoke_user_oauth removes the persisted token row."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_rev",
                "user_code": "BYE-0001",
                "interval": 5,
            },
            url=str(url),
        )

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {
                "access_token": "eyJ.x.y",
                "refresh_token": "rt",
                "id_token": "eyJ.z",
                "account_id": "acc",
            },
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1", bound_owner_id="u_1")
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        await poll_device_code(user_id="u_1")

    await revoke_user_oauth(user_id="u_1")

    client = boto3.client("dynamodb", region_name="us-east-1")
    resp = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "u_1"}})
    assert "Item" not in resp


@pytest.mark.asyncio
async def test_resolve_token_owner_returns_connector_for_org():
    with patch(
        "core.services.oauth_service.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"owner_id": "org_1", "oauth_connector_user_id": "user_a"}),
    ):
        assert await oauth_service.resolve_token_owner(owner_id="org_1") == "user_a"


@pytest.mark.asyncio
async def test_resolve_token_owner_falls_back_to_owner_id():
    # Personal owners: owner_id IS the Clerk user id. Legacy rows and
    # missing rows both fall back.
    with patch(
        "core.services.oauth_service.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=None),
    ):
        assert await oauth_service.resolve_token_owner(owner_id="user_b") == "user_b"


@pytest.mark.asyncio
async def test_resolve_token_owner_legacy_row_without_field_falls_back():
    with patch(
        "core.services.oauth_service.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value={"owner_id": "user_b"}),
    ):
        assert await oauth_service.resolve_token_owner(owner_id="user_b") == "user_b"


def test_connector_from_account_returns_connector_when_present():
    account = {"owner_id": "org_1", "oauth_connector_user_id": "user_a"}
    assert oauth_service.connector_from_account(account, "org_1") == "user_a"


# ---------------------------------------------------------------------------
# Token-row binding: one ChatGPT sign-in ↔ one workspace.
#
# The oauth-tokens table keys one row per Clerk user. A human who connects
# ChatGPT for an ORG (their row backs the org container) can also open a
# PERSONAL workspace under the same Clerk user_id. The completed row records
# the workspace owner it was created for (``bound_owner_id``); consumption
# reads pass the owner whose container will use the tokens and get None when
# the row was claimed by a different workspace.
# ---------------------------------------------------------------------------


async def _complete_flow(user_id: str, bound_owner_id: str) -> None:
    """Drive a full device-code completion for ``user_id`` so the row lands
    active with the binding ``bound_owner_id`` stamped on it."""

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev", "user_code": "CODE-1", "interval": 5}, url=str(url))

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {
                "access_token": "eyJ.x.y",
                "refresh_token": "rt",
                "id_token": "eyJ.z",
                "account_id": "acc",
            },
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id=user_id, bound_owner_id=bound_owner_id)
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        await poll_device_code(user_id=user_id)


@pytest.mark.asyncio
async def test_request_device_code_stamps_binding_on_pending_row(oauth_table):
    """The binding is fixed at session start, where the owner gate runs. /start
    stamps bound_owner_id onto the PENDING row so completion can't re-target it."""

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_p", "user_code": "PEND-1", "interval": 5}, url=str(url))

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="user_a", bound_owner_id="org_1")

    client = boto3.client("dynamodb", region_name="us-east-1")
    row = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "user_a"}})["Item"]
    assert row["state"]["S"] == "pending"
    assert row["bound_owner_id"]["S"] == "org_1"


@pytest.mark.asyncio
async def test_poll_reads_binding_from_pending_row_and_returns_it(oauth_table):
    """poll_device_code reads the binding off the pending row (stamped at /start)
    and (a) stamps it on the active row, (b) surfaces it on DevicePollResult so
    the router learns what the session was actually authorized for — independent
    of who is polling."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    # Re-run a completion to capture the return value directly.
    await revoke_user_oauth(user_id="user_a")

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_b", "user_code": "BND-1", "interval": 5}, url=str(url))

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {"access_token": "eyJ.x.y", "refresh_token": "rt", "id_token": "eyJ.z", "account_id": "acc"},
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="user_a", bound_owner_id="org_1")
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        result = await poll_device_code(user_id="user_a")

    assert isinstance(result, DevicePollResult)
    assert result.bound_owner_id == "org_1"

    client = boto3.client("dynamodb", region_name="us-east-1")
    row = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "user_a"}})["Item"]
    assert row["bound_owner_id"]["S"] == "org_1"


@pytest.mark.asyncio
async def test_poll_legacy_pending_row_without_binding_falls_back_to_user_id(oauth_table):
    """A pending row written before this change has no bound_owner_id. poll falls
    back to user_id (personal semantics) so an in-flight session at deploy time
    completes cleanly instead of erroring or binding to an empty owner."""
    # Hand-write a pending row in the new device_auth_id shape but WITHOUT the
    # binding field (mid-deploy: started on old code, polled on new code).
    boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens").put_item(
        Item={
            "user_id": "user_mid",
            "state": "pending",
            "device_auth_id": "dev_mid",
            "user_code": "MID-1",
            "interval": 5,
        }
    )

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {"access_token": "eyJ.x.y", "refresh_token": "rt", "id_token": "eyJ.z", "account_id": "acc"},
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        result = await poll_device_code(user_id="user_mid")

    assert isinstance(result, DevicePollResult)
    assert result.bound_owner_id == "user_mid"

    client = boto3.client("dynamodb", region_name="us-east-1")
    row = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "user_mid"}})["Item"]
    assert row["bound_owner_id"]["S"] == "user_mid"


@pytest.mark.asyncio
async def test_completed_poll_writes_bound_owner_id(oauth_table):
    """A completed device-code flow stamps the workspace owner the session was
    started for (fixed at /start) onto the token row."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    client = boto3.client("dynamodb", region_name="us-east-1")
    row = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "user_a"}})["Item"]
    assert row["bound_owner_id"]["S"] == "org_1"


@pytest.mark.asyncio
async def test_get_decrypted_tokens_returns_row_when_bound_to_matches(oauth_table):
    """The connector's row is bound to the org; reading with bound_to=<org>
    (the container owner) returns the tokens."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    tokens = await get_decrypted_tokens(user_id="user_a", bound_to="org_1")
    assert tokens is not None
    assert tokens["refresh_token"] == "rt"


@pytest.mark.asyncio
async def test_get_decrypted_tokens_returns_none_when_bound_to_differs(oauth_table):
    """THE P1 GUARD: the same connector's row is bound to an org; reading it
    for a DIFFERENT owner (the connector's personal workspace) must return
    None so personal provisioning never reuses the org-claimed tokens."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    tokens = await get_decrypted_tokens(user_id="user_a", bound_to="user_a")
    assert tokens is None


def _seed_legacy_unbound_active_row(user_id: str) -> None:
    """Seed an active row WITHOUT bound_owner_id (pre-binding legacy shape)."""
    from core.encryption import get_fernet

    plain = json.dumps({"access_token": "a", "refresh_token": "r", "account_id": "acc"}).encode()
    boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens").put_item(
        Item={
            "user_id": user_id,
            "state": "active",
            "encrypted_tokens": get_fernet().encrypt(plain),
            "account_id": "acc",
        }
    )


@pytest.mark.asyncio
async def test_get_decrypted_tokens_legacy_unbound_row_admits_own_personal_owner(oauth_table):
    """A row WITHOUT bound_owner_id (pre-binding legacy) is treated as implicitly
    bound to its own user_id. A personal owner's bound_to EQUALS the row's
    user_id, so existing personal users keep working through the cutover."""
    _seed_legacy_unbound_active_row("user_legacy")

    assert await get_decrypted_tokens(user_id="user_legacy", bound_to="user_legacy") is not None


@pytest.mark.asyncio
async def test_get_decrypted_tokens_legacy_unbound_row_rejected_for_org(oauth_table):
    """THE FINDING-1 GUARD: a legacy unbound row predates orgs-on-chatgpt; it can
    only legitimately back the personal workspace of the human who created it.
    An ORG consuming it (bound_to=org_…) must be REJECTED — otherwise the org
    silently shares that human's personal ChatGPT account, the exact hole the
    binding exists to prevent. The org must complete a fresh, org-bound sign-in."""
    _seed_legacy_unbound_active_row("user_legacy")

    assert await get_decrypted_tokens(user_id="user_legacy", bound_to="org_legacy") is None


@pytest.mark.asyncio
async def test_get_decrypted_tokens_no_bound_to_skips_check(oauth_table):
    """bound_to=None (caller didn't specify) keeps the pre-binding behavior —
    the row is returned regardless of its bound_owner_id."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    assert await get_decrypted_tokens(user_id="user_a") is not None


# ---------------------------------------------------------------------------
# /start conflict resolution: an active row blocks a new device-code session,
# but "active here" and "active for a DIFFERENT workspace" are distinct. The
# former is genuinely already-connected (OAuthAlreadyActiveError → 409, which
# the wizard treats as completion); the latter means THIS workspace is NOT
# connected and the wizard must surface a real error (OAuthBoundElsewhereError).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_device_code_active_row_same_workspace_raises_already_active(oauth_table):
    """An active row bound to the SAME workspace the start is for → genuine
    already-connected → OAuthAlreadyActiveError (the 409 the wizard expects)."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_x", "user_code": "OK-9", "interval": 5}, url=str(url))

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthAlreadyActiveError):
            await request_device_code(user_id="user_a", bound_owner_id="org_1")


@pytest.mark.asyncio
async def test_request_device_code_active_row_other_workspace_raises_bound_elsewhere(oauth_table):
    """An active row bound to a DIFFERENT workspace → the current workspace is
    NOT connected; raise OAuthBoundElsewhereError so the wizard shows a real
    error instead of (via a 409) advancing with no usable tokens."""
    await _complete_flow(user_id="user_a", bound_owner_id="org_1")

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_x", "user_code": "OK-9", "interval": 5}, url=str(url))

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthBoundElsewhereError):
            await request_device_code(user_id="user_a", bound_owner_id="org_2")


@pytest.mark.asyncio
async def test_request_device_code_legacy_unbound_active_personal_start_raises_already_active(oauth_table):
    """A legacy unbound active row is implicitly bound to its own user_id. A
    PERSONAL start for that same user (bound_owner_id == user_id) is therefore
    already-connected → OAuthAlreadyActiveError."""
    _seed_legacy_unbound_active_row("user_legacy")

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_x", "user_code": "OK-9", "interval": 5}, url=str(url))

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthAlreadyActiveError):
            await request_device_code(user_id="user_legacy", bound_owner_id="user_legacy")


@pytest.mark.asyncio
async def test_request_device_code_legacy_unbound_active_org_start_raises_bound_elsewhere(oauth_table):
    """The same legacy unbound active row, but the SAME human now starts in ORG
    context (bound_owner_id=org_…). The implicit personal binding ≠ the org, so
    this is bound-elsewhere — the org must complete a fresh, org-bound sign-in."""
    _seed_legacy_unbound_active_row("user_legacy")

    async def fake_seed(self, url, **kwargs):
        return _ok({"device_auth_id": "dev_x", "user_code": "OK-9", "interval": 5}, url=str(url))

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthBoundElsewhereError):
            await request_device_code(user_id="user_legacy", bound_owner_id="org_legacy")


@pytest.mark.asyncio
async def test_has_active_oauth_true_without_decrypting(oauth_table):
    """Connection-state check returns True for an active, bound row WITHOUT
    decrypting the blob — proven by storing undecryptable bytes."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={
            "user_id": {"S": "u_1"},
            "state": {"S": "active"},
            "bound_owner_id": {"S": "u_1"},
            "encrypted_tokens": {"B": b"undecryptable-garbage"},
        },
    )
    assert await oauth_service.has_active_oauth(user_id="u_1", bound_to="u_1") is True


@pytest.mark.asyncio
async def test_has_active_oauth_false_when_bound_elsewhere(oauth_table):
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={"user_id": {"S": "u_1"}, "state": {"S": "active"}, "bound_owner_id": {"S": "org_other"}},
    )
    assert await oauth_service.has_active_oauth(user_id="u_1", bound_to="u_1") is False


@pytest.mark.asyncio
async def test_has_active_oauth_false_when_pending(oauth_table):
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={"user_id": {"S": "u_1"}, "state": {"S": "pending"}},
    )
    assert await oauth_service.has_active_oauth(user_id="u_1", bound_to="u_1") is False


@pytest.mark.asyncio
async def test_poll_refuses_changed_workspace_before_exchange(oauth_table):
    from core.services.oauth_service import OAuthBoundElsewhereError

    oauth_service._table().put_item(
        Item={
            "user_id": "u_scope",
            "state": "pending",
            "bound_owner_id": "org_original",
            "device_auth_id": "device",
            "user_code": "code",
        }
    )
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as exchange:
        with pytest.raises(OAuthBoundElsewhereError):
            await poll_device_code(user_id="u_scope", authorized_owner_id="org_other")
    exchange.assert_not_awaited()
    assert oauth_service._table().get_item(Key={"user_id": "u_scope"})["Item"]["state"] == "pending"


@pytest.mark.asyncio
async def test_duplicate_poll_keeps_existing_tokens_without_exchange(oauth_table):
    oauth_service._table().put_item(
        Item={
            "user_id": "u_duplicate",
            "state": "active",
            "bound_owner_id": "org_original",
            "account_id": "acc",
            "encrypted_tokens": "preserved",
        }
    )
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock) as exchange:
        result = await poll_device_code(user_id="u_duplicate", authorized_owner_id="org_original")
    exchange.assert_not_awaited()
    assert result.account_id == "acc"
    assert oauth_service._table().get_item(Key={"user_id": "u_duplicate"})["Item"]["encrypted_tokens"] == "preserved"


@pytest.mark.asyncio
async def test_stale_completion_cannot_overwrite_new_device_session(oauth_table):
    table = oauth_service._table()
    table.put_item(
        Item={
            "user_id": "u_stale",
            "state": "pending",
            "bound_owner_id": "org_original",
            "device_auth_id": "old",
            "user_code": "code",
        }
    )

    async def complete_then_replace(self, url, **kwargs):
        if "deviceauth/token" in str(url):
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, str(url))
        table.put_item(
            Item={
                "user_id": "u_stale",
                "state": "pending",
                "bound_owner_id": "org_original",
                "device_auth_id": "new",
                "user_code": "new-code",
            }
        )
        return _ok({"access_token": "eyJ.x.y", "refresh_token": "rt", "account_id": "acc"}, str(url))

    with patch.object(httpx.AsyncClient, "post", new=complete_then_replace):
        with pytest.raises(OAuthExchangeFailedError, match="session changed"):
            await poll_device_code(user_id="u_stale", authorized_owner_id="org_original")
    row = table.get_item(Key={"user_id": "u_stale"})["Item"]
    assert row["device_auth_id"] == "new"
    assert "encrypted_tokens" not in row
