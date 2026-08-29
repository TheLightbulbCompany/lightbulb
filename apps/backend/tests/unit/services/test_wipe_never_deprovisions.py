"""Spec §3.6: teardown revokes OUR link and deletes OUR credential copies. It
NEVER calls Projects `remove` or touches an owner-owned provider resource."""

import inspect
import os
import re
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import account_wipe, entitlement_teardown, owner_secret_sweep  # noqa: E402
from core.services.account_wipe import wipe_account  # noqa: E402
from core.services.company_stripe import deauthorize as real_company_stripe_deauthorize  # noqa: E402
from tests.unit.services.test_account_wipe import OWNER_ID, wipe_mocks  # noqa: E402, F401


# Tokenized so a future rewrite as ``["projects", "remove"]`` (the list form
# `subprocess.run(["stripe", "projects", "remove", ...])` would actually take)
# is still caught, not just the literal joined string.
_PROJECTS_REMOVE_TOKENS = re.compile(r"""["']projects["']\s*,\s*["']remove["']""")


def test_no_teardown_module_can_reach_the_provisioning_rail():
    """Static guard, and the cheapest one: nothing in teardown may import the
    provisioning modules, reach for the CLI's own revoke, or shell out — so it
    cannot deprovision even by accident. If a future change needs one of these
    here, that is a design decision to argue for — not something to slip past
    by deleting this test."""
    for module in (account_wipe, entitlement_teardown, owner_secret_sweep):
        source = inspect.getsource(module)
        assert "provisioning_landing" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "provisioning_approvals" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "revoke_credentials" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "subprocess" not in source, f"{module.__name__} can now shell out"
        assert "projects remove" not in source
        assert not _PROJECTS_REMOVE_TOKENS.search(source), f"{module.__name__} carries a tokenized 'projects remove'"


def test_wipe_still_deletes_our_own_copies():
    """The complement: our Secrets Manager copies and integration rows DO go
    (steps 9f/9g, account_wipe.py:910-920)."""
    source = inspect.getsource(account_wipe)
    assert "delete_owner_secrets" in source
    assert "integrations_repo.delete_all_for_owner" in source


@pytest.mark.asyncio
async def test_wipe_deletes_our_copies_but_never_reaches_the_provider(wipe_mocks):  # noqa: F811
    """Behavioural half of the two guards above: run the REAL ``wipe_account``
    against an owner who provisioned a Stripe Projects resource (spec §3.6 --
    an ``integrations`` row with ``actor="projects"`` + ``provider_resources``,
    the shape ``provisioning_landing.land_credentials`` writes) and a Secrets
    Manager blob under the integrations prefix holding the provider's token.

    ``provisioning_landing.revoke_credentials`` and ``subprocess.run`` are
    patched as spies -- neither module can legitimately reach either, so this
    proves it at runtime rather than just structurally. The real
    ``company_stripe.deauthorize`` is let through (overriding wipe_mocks'
    default full stub) so the only Stripe SDK call it can make -- the P0a
    Connect ``stripe.OAuth.deauthorize`` -- is the one actually observed;
    any OTHER stripe client call (e.g. a hypothetical resource delete) would
    show up in the same spy and fail the exact-calls assertion below.
    """
    # wipe_account deletes integration rows via the single wholesale
    # `integrations_repo.delete_all_for_owner` call (already stubbed by
    # wipe_mocks); it never re-lists rows itself (that's preview_wipe's job).
    # This return value simulates the owner's one Stripe-Projects-actor row --
    # `provider_resources`-shaped per `provisioning_landing.land_credentials`
    # -- being what got deleted.
    wipe_mocks.integrations_delete.return_value = ["stripe"]
    wipe_mocks.owner_secrets_delete.return_value = [f"lightbulb/dev/integrations/{OWNER_ID}/stripe"]
    # Escape wipe_mocks' default full stub for just this one collaborator so
    # the real function body runs and can be caught reaching for more than
    # the P0a deauthorize.
    wipe_mocks.company_stripe_deauthorize.side_effect = real_company_stripe_deauthorize

    with (
        patch(
            "core.services.provisioning_landing.revoke_credentials",
            new_callable=AsyncMock,
        ) as revoke_spy,
        patch("subprocess.run") as subprocess_spy,
        patch("core.services.company_stripe.stripe") as stripe_spy,
    ):
        report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert all(s.status == "ok" for s in report.steps), report.steps

    # Our copies go.
    wipe_mocks.owner_secrets_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.integrations_delete.assert_awaited_once_with(OWNER_ID)

    # The provider is never reached: no CLI revoke, no shell-out, and the
    # ONLY Stripe SDK call made anywhere in the wipe is the P0a Connect
    # deauthorize -- exact-calls, not just "not this one call", so a stray
    # extra call (e.g. a Projects-shaped delete) fails this too.
    revoke_spy.assert_not_awaited()
    subprocess_spy.assert_not_called()
    assert len(stripe_spy.mock_calls) == 1
    [deauth_call] = stripe_spy.mock_calls
    assert deauth_call[0] == "OAuth.deauthorize"
    assert deauth_call.kwargs == {
        "client_id": settings.STRIPE_CONNECT_CLIENT_ID,
        "stripe_user_id": "acct_xyz789",
    }
