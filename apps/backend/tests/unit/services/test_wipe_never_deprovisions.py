"""Spec §3.6: teardown revokes OUR link and deletes OUR credential copies. It
NEVER calls Projects `remove` or touches an owner-owned provider resource."""

import inspect
import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import account_wipe, entitlement_teardown, owner_secret_sweep  # noqa: E402


def test_no_teardown_module_can_reach_the_provisioning_rail():
    """Static guard, and the cheapest one: nothing in teardown may import the
    provisioning modules, so it cannot deprovision even by accident. If a future
    change needs one of them here, that is a design decision to argue for — not
    something to slip past by deleting this test."""
    for module in (account_wipe, entitlement_teardown, owner_secret_sweep):
        source = inspect.getsource(module)
        assert "provisioning_landing" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "provisioning_approvals" not in source, f"{module.__name__} can now reach the provisioning rail"
        assert "projects remove" not in source


def test_wipe_still_deletes_our_own_copies():
    """The complement: our Secrets Manager copies and integration rows DO go
    (steps 9f/9g, account_wipe.py:910-920)."""
    source = inspect.getsource(account_wipe)
    assert "delete_owner_secrets" in source
    assert "integrations_repo.delete_all_for_owner" in source
