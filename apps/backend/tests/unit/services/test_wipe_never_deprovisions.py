"""Spec §3.6: teardown deletes OUR copies and NEVER deprovisions.

An account wipe removes the pointer row, the Secrets Manager blob and our
memory of the ask. The resource itself lives in the owner's own provider
account, bought against the owner's own Stripe payment method — it is not
ours to delete, and `remove` is reachable from exactly one place in the
codebase: the owner's own typed-confirmation route.
"""

import inspect
import os
import re
from unittest.mock import ANY, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.config import settings  # noqa: E402
from core.services import account_wipe, entitlement_teardown, owner_secret_sweep, workspace_wipe  # noqa: E402
from core.services.account_wipe import wipe_account  # noqa: E402
from core.repositories import integrations_repo  # noqa: E402
from core.services.company_stripe import deauthorize as real_company_stripe_deauthorize  # noqa: E402
from core.services.credentials import integration_secret_store  # noqa: E402
from routers import connections  # noqa: E402
from tests.unit.services.test_account_wipe import OWNER_ID, wipe_mocks  # noqa: E402, F401

# What this rail's blob is actually NAMED, computed the same way the writer
# computes it — not a literal that could drift from the namespace helper.
_PROJECTS_SECRET_NAME = integration_secret_store._secret_name(OWNER_ID, integrations_repo.projects_key("posthog"))


# Tokenized so a rewrite as ``["projects", "remove"]`` (the list form a
# `subprocess.run` shell-out would actually take) is still caught, not just
# the literal joined string.
_PROJECTS_REMOVE_TOKENS = re.compile(r"""["']projects["']\s*,\s*["']remove["']""")

# `connections`: revoke-all runs its Disconnect teardown for every connected app.
_TEARDOWN_MODULES = (
    account_wipe,
    workspace_wipe,
    entitlement_teardown,
    owner_secret_sweep,
    connections,
)

# The modules that CAN deprovision, and the three call names that do it.
# `provisioning_workflow_repo` is deliberately absent: the wipe closes the
# owner's open rows with a conditional DDB write, and the repo cannot reach
# Stripe at all. Everything else on the rail can.
_FORBIDDEN = (
    "stripe_provisioning",
    "provisioning_workflow",
    "provisioning_landing",
    "provisioning_approvals",
    "apps_service",
    "apps_sweeper",
    "remove_resource",
    "unlink_resource",
    "rotate_credentials",
    "revoke_credentials",
    "subprocess",
)


def test_no_teardown_module_can_reach_a_provider():
    """Spec §3.6: teardown deletes OUR copies and NEVER calls Provisioning
    ``remove``/``unlink``. The cheapest guarantee is that teardown cannot
    reach the module that can. If a future change needs one of these imports
    here, that is a design decision to argue for — not something to slip past
    by deleting this test."""
    for module in _TEARDOWN_MODULES:
        # Blank the ONE allowed name before matching, so the
        # `provisioning_workflow` entry above still bites on the state
        # machine (of which it is a prefix) rather than being defeated by it.
        source = inspect.getsource(module).replace("provisioning_workflow_repo", "«allowed: the repo»")
        for name in _FORBIDDEN:
            assert name not in source, f"{module.__name__} can now reach {name}"
        assert "projects remove" not in source
        assert not _PROJECTS_REMOVE_TOKENS.search(source), f"{module.__name__} carries a tokenized 'projects remove'"


def test_wipe_still_deletes_our_own_copies():
    """The complement: our Secrets Manager copies and integration rows DO go
    (steps 9f/9g)."""
    source = inspect.getsource(account_wipe)
    assert "delete_owner_secrets" in source
    assert "integrations_repo.delete_all_for_owner" in source


def test_the_secret_sweep_prefix_covers_the_namespaced_projects_blob():
    """The wipe does NOT enumerate providers — ``delete_owner_secrets`` lists
    Secrets Manager by owner PREFIX. So the `projects/{provider}` namespace is
    swept for free, exactly like ``oauth_app_store``'s `oauth-app/{provider}`,
    and needs no prefix handling of its own. This is the assertion that fails
    if either the namespace or the sweep prefix ever moves apart."""
    prefixes = owner_secret_sweep.owner_secret_prefixes(OWNER_ID)
    assert any(_PROJECTS_SECRET_NAME.startswith(prefix) for prefix in prefixes), (
        f"{_PROJECTS_SECRET_NAME} is under none of {prefixes}"
    )


@pytest.mark.asyncio
async def test_wipe_deletes_our_copies_but_never_reaches_the_provider(wipe_mocks):  # noqa: F811
    """Behavioural half of the two guards above: run the REAL ``wipe_account``
    against an owner who provisioned a Stripe Projects resource (spec §3.6 --
    an ``integrations`` row with ``actor="projects"`` + ``provider_resources``,
    the shape ``provisioning_landing.land_credentials`` writes), a Secrets
    Manager blob under `projects/posthog` holding the provider's token, and an
    OPEN ``provisioning-workflows`` row that already carries a ``resource_id``.

    Every function on the rail that could deprovision is patched as a spy --
    none of them is reachable from teardown, so this proves at RUNTIME what
    the source guards above prove statically. The real
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
    wipe_mocks.integrations_delete.return_value = [integrations_repo.projects_key("posthog")]
    wipe_mocks.owner_secrets_delete.return_value = [_PROJECTS_SECRET_NAME]
    # An open row mid-`provisioning`, with the resource already created. This
    # is deliberately a row `provisioning_workflow.cancel` would REFUSE (it
    # guards on `resource_id` so an owner's withdrawal can never orphan a
    # live resource) -- the wipe is the one caller that must close it anyway,
    # which is why it goes through the repo's conditional write instead.
    wipe_mocks.workflows_list.return_value = [
        {"workflow_id": "pwf_1", "owner_id": OWNER_ID, "state": "provisioning", "resource_id": "prv_res_1"}
    ]
    # Escape wipe_mocks' default full stub for just this one collaborator so
    # the real function body runs and can be caught reaching for more than
    # the P0a deauthorize.
    wipe_mocks.company_stripe_deauthorize.side_effect = real_company_stripe_deauthorize

    with (
        # A configured client id, so the deauthorize is actually attempted and
        # the exact-calls assertion below keeps its teeth. Unset (the Settings
        # default) it is skipped by design — see `company_stripe.deauthorize`.
        patch.object(settings, "STRIPE_CONNECT_CLIENT_ID", "ca_test"),
        patch("subprocess.run") as subprocess_spy,
        patch("core.services.company_stripe.stripe") as stripe_spy,
    ):
        report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    assert all(s.status == "ok" for s in report.steps), report.steps

    # Our copies go — including the rail's NAMESPACED row and blob, which ride
    # the same two wholesale calls (row: `delete_all_for_owner` deletes each
    # listed `row["provider"]`, so `projects/posthog` flows through; secret:
    # the owner-prefix sweep, see the test above).
    wipe_mocks.owner_secrets_delete.assert_awaited_once_with(OWNER_ID)
    wipe_mocks.integrations_delete.assert_awaited_once_with(OWNER_ID)
    details = {s.system: s.detail for s in report.steps}
    assert integrations_repo.projects_key("posthog") in details["integrations"]
    assert "1 owner secret(s)" in details["owner_secrets"]

    # And our MEMORY of the ask goes: the open row is closed, so no later read
    # of it ever touches Stripe for this owner again. A pure DDB write — `to_state="cancelled"`, no fields, no Stripe.
    wipe_mocks.workflows_transition.assert_awaited_once_with("pwf_1", from_state="provisioning", to_state="cancelled")
    assert "cancelled 1 of 1" in details["provisioning"]

    # The provider is never reached: no shell-out, and the ONLY
    # Stripe SDK call made anywhere in the wipe is the P0a Connect
    # deauthorize -- exact-calls, not just "not this one call", so a stray
    # extra call (e.g. a Projects-shaped delete) fails this too.
    subprocess_spy.assert_not_called()
    assert len(stripe_spy.mock_calls) == 1
    [deauth_call] = stripe_spy.mock_calls
    assert deauth_call[0] == "OAuth.deauthorize"
    # The literal, not `settings.…`: the patch above is out of scope by now.
    assert deauth_call.kwargs == {"client_id": "ca_test", "stripe_user_id": "acct_xyz789"}


@pytest.mark.asyncio
async def test_wipe_leaves_a_row_another_writer_already_closed(wipe_mocks):  # noqa: F811
    """A terminal row is not re-closed, and a row a concurrent reader moved out
    from under us (``transition`` returns None) is not an error — the wipe
    reports what it actually closed and carries on."""
    wipe_mocks.workflows_list.return_value = [
        {"workflow_id": "pwf_done", "owner_id": OWNER_ID, "state": "active"},
        {"workflow_id": "pwf_raced", "owner_id": OWNER_ID, "state": "binding"},
    ]
    wipe_mocks.workflows_transition.return_value = None

    report = await wipe_account(OWNER_ID, actor_admin_id="admin_1")

    detail = {s.system: s for s in report.steps}["provisioning"]
    assert detail.status == "ok"
    assert "cancelled 0 of 1" in detail.detail
    wipe_mocks.workflows_transition.assert_awaited_once_with("pwf_raced", from_state="binding", to_state=ANY)
