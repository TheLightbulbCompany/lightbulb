"""SP3 Pass 1 — negative / auth fuzzing over the whole API surface.

Fires generated malformed/hostile requests at every operation with NO auth
token, in-process over ASGI under moto. Asserts schemathesis's default checks
(not_a_server_error, response_schema_conformance, status_code_conformance,
content_type_conformance) — i.e. no 5xx on garbage, and responses conform to
the declared contract. Unauthenticated requests bounce at the auth/validation
layer before any external call, so the pass is hermetic.

We assert ONLY ``not_a_server_error`` (no 5xx). schemathesis v4's default set
also includes positive_data_acceptance / response_schema_conformance /
status_code_conformance, but those require the OpenAPI document to fully
declare every error response (401/403/422/400). Lightbulb AI doesn't, so those checks
fire on every undocumented 4xx — high-noise, not bugs. The 5xx check is the
resilience signal: any 500 on hostile input is a real unhandled-exception bug.
The under-documented error responses are recorded as an SP3 finding, not gated."""

import pytest
from hypothesis import HealthCheck, settings

from schemathesis.checks import not_a_server_error

from tests.chaos.api.conftest import NEGATIVE_PASS_EXCLUSIONS, schema


@schema.parametrize()
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
)
@pytest.mark.chaos
def test_contract_negative(case, chaos_ddb):
    # Key on the stable operation label, not case.method/path — schemathesis
    # mutates the HTTP method during fuzzing.
    label = case.operation.label
    if label in NEGATIVE_PASS_EXCLUSIONS:
        pytest.skip(f"excluded: {NEGATIVE_PASS_EXCLUSIONS[label]}")
    case.call_and_validate(checks=[not_a_server_error])
