"""Receipt registration is best-effort and issue-scoped: no issue or no
company skips cleanly, and no failure ever escapes to the caller."""

import os
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import work_products as mod  # noqa: E402


def _admin(create=None):
    admin = MagicMock()
    admin._http = MagicMock()
    admin.create_issue_work_product = create or AsyncMock(return_value={"id": "wp_1"})
    return admin


@contextmanager
def _patched(admin, company_id="comp_1"):
    with ExitStack() as stack:
        stack.enter_context(patch.object(mod, "PaperclipRepo", MagicMock()))
        stack.enter_context(patch.object(mod, "resolve_company_id_for_owner", AsyncMock(return_value=company_id)))
        stack.enter_context(patch.object(mod, "shared_admin_client", MagicMock(return_value=admin)))
        stack.enter_context(patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="s=1")))
        yield


@pytest.mark.asyncio
async def test_registers_with_the_camelcase_wire_body():
    admin = _admin()
    with _patched(admin):
        out = await mod.register(
            "org_a",
            type="artifact",
            provider="posthog",
            title="Provisioned svc_analytics",
            issue_id="iss_1",
            external_id="res_1",
            metadata={"workflow_id": "pwf_1", "level": "company"},
        )
    assert out == "wp_1"
    call = admin.create_issue_work_product.await_args
    assert call.kwargs["session_cookie"] == "s=1"
    assert call.kwargs["issue_id"] == "iss_1"
    assert call.kwargs["body"] == {
        "type": "artifact",
        "provider": "posthog",
        "title": "Provisioned svc_analytics",
        "reviewState": "none",
        "externalId": "res_1",
        "metadata": {"workflow_id": "pwf_1", "level": "company"},
    }


@pytest.mark.asyncio
async def test_no_issue_id_skips_without_any_paperclip_call():
    admin = _admin()
    resolve = AsyncMock(return_value="comp_1")
    with (
        patch.object(mod, "resolve_company_id_for_owner", resolve),
        patch.object(mod, "shared_admin_client", MagicMock(return_value=admin)),
    ):
        out = await mod.register("org_a", type="artifact", provider="posthog", title="t", issue_id=None)
    assert out is None
    resolve.assert_not_awaited()
    admin.create_issue_work_product.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_paperclip_company_skips_before_the_admin_client():
    admin = _admin()
    with _patched(admin, company_id=None):
        out = await mod.register("org_a", type="artifact", provider="posthog", title="t", issue_id="iss_1")
    assert out is None
    admin.create_issue_work_product.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_receipt_never_raises():
    admin = _admin(create=AsyncMock(side_effect=RuntimeError("paperclip down")))
    with _patched(admin):
        out = await mod.register("org_a", type="artifact", provider="posthog", title="t", issue_id="iss_1")
    assert out is None
