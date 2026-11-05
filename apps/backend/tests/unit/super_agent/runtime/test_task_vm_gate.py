"""Spec 13 §5.4: the one rule deciding whether a Worker task gets its own VM. False while any
rotating credential in the workspace is still VM-custodied (ChatGPT login without a backend
grant, or a connected X/QuickBooks row without ``custody: backend``); Instagram never blocks."""

from unittest.mock import AsyncMock

import pytest

from core.repositories import integrations_repo
from core.services import oauth_service, token_custody

OWNER = "org_gate"


def _row(provider: str, *, status: str = "connected", custody: str | None = None) -> dict:
    row = {"owner_id": OWNER, "provider": provider, "status": status}
    if custody:
        row["custody"] = custody
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "token_custody_attr", "rows", "expected"),
    [
        ("chatgpt_oauth", None, [], False),  # ChatGPT login, grant still in the VM's auth.json
        ("chatgpt_oauth", "backend", [], True),  # ChatGPT login, brokered
        ("chatgpt_oauth", "missing", [], False),  # ChatGPT login with no token row yet
        ("openrouter_managed", None, [_row("x")], False),  # X connected, VM rotates its token
        ("openrouter_managed", None, [_row("x", custody="backend")], True),
        ("openrouter_managed", None, [_row("quickbooks", custody="backend")], True),
        ("openrouter_managed", None, [_row("quickbooks")], False),
        ("openrouter_managed", None, [_row("instagram")], True),  # no single-use token: never blocks
        ("openrouter_managed", None, [], True),
        ("byo_api_key", None, [_row("stripe")], True),  # backend-refreshed providers never block
        ("openrouter_managed", None, [_row("x", status="disconnected")], True),
        ("openrouter_managed", None, [_row("x", status="reconnect_required")], True),
        ("chatgpt_oauth", "backend", [_row("x")], False),  # brokered ChatGPT, but X is not
    ],
)
async def test_gate_truth_table(monkeypatch, choice, token_custody_attr, rows, expected):
    monkeypatch.setattr(token_custody, "resolve_provider_choice", AsyncMock(return_value=(choice, None)))
    monkeypatch.setattr(oauth_service, "resolve_token_owner", AsyncMock(return_value="user_g"))
    token_row = None if token_custody_attr == "missing" else {"tokens": {}, "custody": token_custody_attr}
    if token_row is not None and token_custody_attr is None:
        token_row.pop("custody")
    get_row = AsyncMock(return_value=token_row)
    monkeypatch.setattr(oauth_service, "get_token_row", get_row)
    list_rows = AsyncMock(return_value=rows)
    monkeypatch.setattr(integrations_repo, "list_for_owner", list_rows)

    assert await token_custody.task_vm_allowed(OWNER) is expected

    # One consistent read each, bound to the workspace, every call (never cached). A ChatGPT grant
    # still in the VM answers False before the integrations are even read.
    if choice == "chatgpt_oauth":
        get_row.assert_awaited_once_with("user_g", consistent=True, bound_to=OWNER)
    else:
        get_row.assert_not_awaited()
    chatgpt_blocks = choice == "chatgpt_oauth" and token_custody_attr != "backend"
    if chatgpt_blocks:
        list_rows.assert_not_awaited()
    else:
        list_rows.assert_awaited_once_with(OWNER, consistent=True)
