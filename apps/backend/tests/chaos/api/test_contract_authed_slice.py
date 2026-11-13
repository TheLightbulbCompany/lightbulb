"""SP3 Pass 2 — curated authenticated deep slice.

Pass 1 sees auth-gated endpoints bounce at 401. This pass overrides
get_current_user (a personal, no-org test user) + moto and fuzzes a small
ALLOWLIST of pure-DDB endpoints so their handler logic is actually exercised
with valid auth. Asserts not_a_server_error.

ALLOWLIST is intentionally minimal and explicit — only endpoints confirmed to
touch nothing beyond the moto-provisioned tables for a personal auth context
(e.g. billing usage reads skip the org member-enrich Clerk call when there's no
org). Grow it as more safe endpoints are confirmed."""

import pytest
from hypothesis import HealthCheck, settings

from schemathesis.checks import not_a_server_error

from tests.chaos.api.conftest import schema

# Operation labels of pure-DDB endpoints safe to fuzz with a personal auth
# context. Keyed on the stable label (schemathesis mutates case.method).
ALLOWLIST: set[str] = {
    "GET /api/v1/billing/usage",  # get_usage_summary — usage-counters only (no-org skips Clerk enrich)
}


@pytest.fixture
def _authed_env(chaos_ddb):
    """moto tables (via chaos_ddb) + a personal authenticated user override."""
    from core.auth import AuthContext, get_current_user

    from tests.chaos.api.conftest import app

    app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id="user_chaos_api")
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


# Narrowed at parametrize time, not by a skip inside the test: schemathesis generates its
# Hypothesis coverage cases for every parametrized operation DURING COLLECTION, so the old
# whole-schema parametrize spent ~16 min of every Backend CI run building cases for ~567
# operations this test then skipped (measured 2026-10-03: 33-55 min of collection per run).
@schema.include(name=sorted(ALLOWLIST)).parametrize()
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
)
@pytest.mark.chaos
def test_contract_authed_slice(case, _authed_env):
    # The include() above is the filter; this guard only catches an over-broad match.
    if case.operation.label not in ALLOWLIST:
        pytest.skip("not in authed deep-slice allowlist")
    case.call_and_validate(checks=[not_a_server_error])
