"""Provisioning idempotency — short-circuit invariant.

The highest-value provisioning invariant is that provision_org is idempotent:
once an active PaperclipCompany row exists for an owner, repeated calls return
it WITHOUT performing any Paperclip HTTP (the documented short-circuit at
paperclip_provisioning.py:226). We prove "no HTTP" by handing it an admin
client that raises on every attribute access.

INVARIANTS:
  * N calls over an existing active row all return that same row (company_id stable)
  * exactly one row remains for the owner
  * zero admin-client (HTTP) methods are invoked on the short-circuit path

NOT covered here (documented follow-up in the findings doc): the full
provision-from-empty chain (sign_up -> create_company -> invite -> create_agent),
which would require faithfully faking ~7 admin methods + the session-cookie
challenge and become mock-dominated."""

from datetime import datetime, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


class _RaisingAdmin:
    """Any attribute access yields an async callable that fails the test —
    proves the short-circuit path performs no Paperclip HTTP."""

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise AssertionError(f"admin.{name} called — short-circuit should perform no HTTP")

        return _boom


@pytest.mark.chaos
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(num_calls=st.integers(min_value=1, max_value=5))
@pytest.mark.asyncio
async def test_provision_org_idempotent_short_circuit(chaos_ddb, num_calls):
    from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
    from core.services.paperclip_provisioning import PaperclipProvisioning

    owner_id = "org_chaos_prov"
    owner_user_id = "user_chaos_prov"
    repo = PaperclipRepo(table_name="paperclip-companies")

    now = datetime.now(timezone.utc)
    seeded = PaperclipCompany(
        user_id=owner_user_id,
        org_id=owner_id,
        company_id="company_chaos_1",
        paperclip_user_id="pc_user_1",
        paperclip_password_encrypted="enc",
        service_token_encrypted="enc",
        status="active",
        created_at=now,
        updated_at=now,
    )
    await repo.put(seeded)  # put overwrites the single key, so no cross-example accumulation

    provisioning = PaperclipProvisioning(_RaisingAdmin(), repo, env_name="dev")

    for _ in range(num_calls):
        result = await provisioning.provision_org(
            owner_id=owner_id,
            owner_user_id=owner_user_id,
            owner_email="chaos@example.com",
        )
        assert result.company_id == "company_chaos_1"
        assert result.status == "active"

    # exactly one row, still active
    row = await repo.get(owner_user_id)
    assert row is not None and row.status == "active" and row.company_id == "company_chaos_1"
