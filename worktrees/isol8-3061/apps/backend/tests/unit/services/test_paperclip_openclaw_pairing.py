"""The Paperclip ROSTER helpers: reading an owner's live agent list, their company id, and
their starter-agent identities.

The pairing half this module was named for is deleted with the OpenClaw gateway
(``pair_with_openclaw``, ``ensure_operator_device_paired``, ``register_paperclip_agent_devices``,
the trust-store approval channel, and the operator keypair they all minted) — a Hermes agent's
"pairing" is its S3 profile, and the native adapter needs no device identity. The 33 tests over
those went with them; the module keeps its filename because renaming it churns 24 import sites
for no behaviour.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.services import paperclip_openclaw_pairing as pairing
from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_openclaw_pairing import (
    fetch_paperclip_agents_for_owner,
    resolve_company_id_for_owner,
)


def _claimed_path(agent_id):
    return f"~/.openclaw/workspace-{agent_id}/paperclip-claimed-api-key.json"


def _mock_admin_and_repo(agents, monkeypatch):
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_x"))
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.patch_agent = AsyncMock()
    admin._http = MagicMock()
    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie"),
    )
    return admin, repo


@pytest.mark.asyncio
async def test_fetch_returns_empty_list_for_personal_user_with_no_paperclip_row():
    """Personal user (owner_id starts with 'user_') has no PaperclipCompany
    row -> return []. Container provision falls back to 'main'."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=None)
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_for_personal_user_returns_their_company_agents(monkeypatch):
    """Personal user happy path: repo.get(user_id) -> company_id ->
    list_agents -> return list."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(return_value=[{"id": pc_uuid, "name": "Friday", "role": "ceo"}])

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == [{"id": pc_uuid, "name": "Friday", "role": "ceo"}]
    admin_client.list_agents.assert_called_once_with(
        session_cookie="cookie123",
        company_id="co_xyz",
    )


@pytest.mark.asyncio
async def test_fetch_for_org_uses_list_by_org_id(monkeypatch):
    """Org owner (owner_id starts with 'org_') resolves company via
    repo.list_by_org_id (the org's ACTIVE row), NOT repo.get and NOT
    repo.get_org_company_id (which would return a failed row's id)."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    repo = MagicMock()
    repo.get = AsyncMock()  # should NOT be called
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "active", "company_id": "co_xyz"}])
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(return_value=[{"id": pc_uuid, "name": "Friday", "role": "ceo"}])

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert len(result) == 1
    repo.get.assert_not_awaited()
    repo.list_by_org_id.assert_awaited_once_with("org_xyz")


@pytest.mark.asyncio
async def test_fetch_for_org_with_no_company_returns_empty():
    """Org never had Paperclip provisioned (no active row) -> return [].
    Container provision falls back to 'main'."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[])
    admin_client = MagicMock()

    result = await fetch_paperclip_agents_for_owner(
        owner_id="org_xyz",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_swallows_availability_failure_by_default(monkeypatch):
    """Default contract: availability failures fall back to [] so container
    provision never breaks because Paperclip is down."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(
        side_effect=PaperclipApiError("list_agents", status_code=500, body="paperclip down")
    )

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )
    assert result == []


@pytest.mark.asyncio
async def test_fetch_raise_on_unavailable_reraises_availability_failure(monkeypatch):
    """Regression (codex): the Friday-publish guard reads [] as "allow", so
    an outage swallowed into [] fails OPEN. raise_on_unavailable=True must
    re-raise the availability failure instead of masking it."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(
        side_effect=PaperclipApiError("list_agents", status_code=500, body="paperclip down")
    )

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    with pytest.raises(PaperclipApiError):
        await fetch_paperclip_agents_for_owner(
            owner_id="user_abc",
            repo=repo,
            admin_client=admin_client,
            raise_on_unavailable=True,
        )


@pytest.mark.asyncio
async def test_fetch_raise_on_unavailable_rejects_non_list_payload(monkeypatch):
    """Regression (codex): a 200 with a non-list payload (the admin client
    maps empty responses to {}) is as unknowable as an outage — strict mode
    must raise, not degrade to [] (which the Friday guard reads as allow).
    Default mode keeps the swallow for provision."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(return_value={})

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    with pytest.raises(PaperclipApiError):
        await fetch_paperclip_agents_for_owner(
            owner_id="user_abc",
            repo=repo,
            admin_client=admin_client,
            raise_on_unavailable=True,
        )

    assert (
        await fetch_paperclip_agents_for_owner(
            owner_id="user_abc",
            repo=repo,
            admin_client=admin_client,
        )
        == []
    )


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_personal_failed_marker():
    """A personal owner whose only row is a net-new ``failed`` marker
    (``company_id=""``) resolves to None — NOT the empty string. Every caller
    guards with ``if company_id is None``, so an "" would slip through into a
    doomed Paperclip call with an empty company id."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="failed", company_id=""))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_personal_failed_row_with_company():
    """Even a ``failed`` row that happens to carry a company_id resolves to
    None: a failed provision is treated as "no company" for pairing/lifecycle."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="failed", company_id="co_partial"))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_org_failed_marker_empty():
    """An org whose only row is a net-new ``failed`` marker (empty company_id)
    resolves to None: list_by_org_id has no active row."""
    repo = MagicMock()
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "failed", "company_id": ""}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_returns_id_for_active_personal_row():
    """Regression guard: a healthy active row still resolves to its company_id."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(status="active", company_id="co_live"))

    assert await resolve_company_id_for_owner(owner_id="user_abc", repo=repo) == "co_live"


@pytest.mark.asyncio
async def test_resolve_company_id_none_for_org_failed_row_with_company():
    """An ORG whose only row is ``failed`` but carries a REAL adopted company_id
    (the reconcile Friday-partial case) must resolve to None — a deliberately-
    failed workspace must not leak its id to the ``if company_id is None``
    callers. ``get_org_company_id`` is mocked to prove the OLD path returned the
    id (the bug this fixes); the fix routes org resolution through
    ``list_by_org_id`` and requires an ACTIVE row, so both are stubbed and the
    result is None regardless of which the implementation calls."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value="co_partial")  # old path returned the id
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "failed", "company_id": "co_partial"}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) is None


@pytest.mark.asyncio
async def test_resolve_company_id_returns_id_for_active_org_row():
    """Regression guard: an ORG with an ``active`` row still resolves to its
    company_id. Green before and after the fix — the happy org path's outcome is
    unchanged, only the lookup mechanism (get_org_company_id → list_by_org_id)."""
    repo = MagicMock()
    repo.get_org_company_id = AsyncMock(return_value="co_live")
    repo.list_by_org_id = AsyncMock(return_value=[{"status": "active", "company_id": "co_live"}])

    assert await resolve_company_id_for_owner(owner_id="org_abc", repo=repo) == "co_live"


@pytest.mark.asyncio
async def test_get_starter_agent_ids_resolves_authoritative_owner_row():
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(
        return_value=MagicMock(starter_agent_ids={"ceo": "agent-ceo", "finance": "agent-fin"})
    )

    assert await pairing.get_starter_agent_ids(owner_id="org_abc", repo=repo) == {"agent-ceo", "agent-fin"}
    repo.resolve_owner_row.assert_awaited_once_with("org_abc")


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_paperclip_api_error(monkeypatch):
    """If Paperclip is down or returns 5xx, don't fail container provision -
    fall back to legacy 'main' agent. Log the error for ops visibility."""
    from core.services.paperclip_admin_client import PaperclipApiError

    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=PaperclipApiError("503", status_code=503, body={}))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_transient_httpx_error(monkeypatch):
    """Transient httpx errors (connect timeouts, DNS failures, etc.) raised
    BEFORE the request reaches Paperclip must also fall back to []. The
    helper's contract is best-effort: container provision must never fail
    because Paperclip is down or the network is flaky."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []


@pytest.mark.asyncio
async def test_fetch_returns_empty_on_admin_cookie_failure(monkeypatch):
    """If credential loading itself fails (e.g. admin session-cookie fetch
    raises before the list_agents request goes out), still fall back to
    []. The fetch must not abort container provision."""
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock()  # never reached

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(side_effect=RuntimeError("admin secret missing")),
    )

    result = await fetch_paperclip_agents_for_owner(
        owner_id="user_abc",
        repo=repo,
        admin_client=admin_client,
    )

    assert result == []
    admin_client.list_agents.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_propagates_unexpected_errors(monkeypatch):
    """The except clause is tightened to ``(PaperclipApiError,
    httpx.HTTPError, RuntimeError)``. Non-network bugs (e.g. KeyError,
    AttributeError, TypeError) inside the helper or the admin client
    must NOT be swallowed as "Paperclip unavailable" — they need to
    surface in CloudWatch so we can fix them.

    A blanket Exception catch would silently mask real programming bugs
    behind a fallback to the legacy 'main' agent path; the resulting
    UX would be "agents disappear from /teams" with no operator signal.
    """
    repo = MagicMock()
    repo.get = AsyncMock(return_value=MagicMock(company_id="co_xyz"))
    admin_client = MagicMock()
    admin_client.list_agents = AsyncMock(side_effect=KeyError("missing-field"))

    monkeypatch.setattr(
        "core.services.paperclip_openclaw_pairing.get_admin_session_cookie",
        AsyncMock(return_value="cookie123"),
    )

    with pytest.raises(KeyError):
        await fetch_paperclip_agents_for_owner(
            owner_id="user_abc",
            repo=repo,
            admin_client=admin_client,
        )
