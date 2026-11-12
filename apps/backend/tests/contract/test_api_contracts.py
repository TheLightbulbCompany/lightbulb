"""
Schemathesis contract tests.

Auto-validates all API endpoints against the OpenAPI specification.
Uses ASGI transport so no running server is needed.

With mocked dependencies, endpoints that require external services
(gateway, DynamoDB, Clerk webhook verification) are excluded from automated
fuzz testing since they cannot be meaningfully tested without infrastructure.
"""

import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error

from main import app


# Signed in as contract_test_user per test (tests/contract/conftest.py
# _signed_in_as_contract_user), not here: an override set at import lived on the
# shared app for the whole process, so any other test's teardown that cleared it
# silently turned this fuzz into a signed-out one (401 everywhere) -- the result
# depended on which tests ran first.

# Exclude endpoints that require infrastructure not available in mock mode:
# - /ws/* endpoints need API Gateway + DynamoDB connection state
# - /webhooks/* need Clerk signature verification + real webhook payloads
# - Agent write operations need gateway for workspace management
schema = schemathesis.openapi.from_asgi("/api/v1/openapi.json", app=app)
schema = (
    schema.exclude(
        path_regex="^/api/v1/ws/",
    )
    .exclude(
        path_regex="^/api/v1/webhooks/",
    )
    .exclude(
        path_regex="^/api/v1/billing/",
    )
    .exclude(
        path_regex="^/api/v1/container/",
    )
    .exclude(
        path_regex="^/api/v1/debug/",
    )
    .exclude(
        path_regex="^/api/v1/channels/",
    )
    .exclude(
        path_regex="^/api/v1/settings/",
    )
    .exclude(
        path_regex="^/api/v1/integrations/",
    )
    # Credential connect routes hit the connections-vault DDB table + do live
    # provider-validation HTTP calls (and the OAuth authorize path reaches
    # Secrets Manager for the BYO client). None are mocked at the contract
    # layer; covered end-to-end by tests/unit/routers/test_connections.py.
    # Consistent with the /channels/ and /integrations/ exclusions.
    .exclude(
        path_regex="^/api/v1/connections",
    )
    # The OAuth callback reaches the oauth-state DDB table, Secrets Manager
    # (BYO client), and a provider token exchange. Only the {provider}/callback
    # redirect is excluded — the device-code /oauth/chatgpt/* endpoints stay
    # (mocked by the autouse fixture in conftest). Covered by test_connections.py.
    .exclude(
        path_regex="^/api/v1/oauth/[^/]+/callback",
    )
    .exclude(
        path_regex="^/api/v1/users/",
    )
    # Admin dashboard endpoints compose several upstream services (Clerk
    # Backend API, PostHog, CloudWatch Logs, DynamoDB) that aren't mocked
    # at the contract-test layer — schemathesis fuzzing occasionally
    # generates inputs that reach unmocked code paths and surfaces as 5xx.
    # Admin endpoints have dedicated unit tests under tests/unit/routers/
    # that mock the specific collaborators end-to-end. Exclude from fuzz
    # for consistency with /billing/ and /container/ exclusions.
    .exclude(
        path_regex="^/api/v1/admin/",
    )
    # Desktop auth requires CLERK_SECRET_KEY (real Clerk credential) to mint
    # sign-in tokens via the upstream Clerk API. CI doesn't set the secret, so
    # the endpoint returns 500 by design ("CLERK_SECRET_KEY not configured").
    # This is consistent with /webhooks/* exclusion — both rely on real Clerk
    # infrastructure that the contract harness can't mock.
    .exclude(
        path_regex="^/api/v1/auth/",
    )
    # Teams BFF endpoints chain Clerk auth -> DDB lookup -> a Better
    # Auth sign-in to upstream Paperclip. The contract harness has none
    # of those, so every fuzzed request reaches unmocked DDB / httpx
    # code and surfaces as 5xx. Teams routers have dedicated unit tests
    # under tests/unit/routers/teams/ that mock each collaborator
    # end-to-end. Excluded for consistency with /billing/ and /admin/.
    .exclude(
        path_regex="^/api/v1/teams/",
    )
    # Public ingress under /hooks/ (webhook automations, Meta) reads DDB on every
    # request, so it 5xxes without a real DDB. Coverage lives in
    # super_agent/automations/test_hooks.py + test_meta_webhook.py.
    .exclude(
        path_regex="^/api/v1/hooks/",
    )
    # Agent-mail container routes (/u/{owner_id}/send, /address, /inbox) are
    # hook-token authed and read the DDB ``containers`` table to resolve the
    # owner's hook_token, on every request — without a real DDB the boto3
    # GetItem raises 5xx. Coverage for these routes lives
    # in tests/unit/routers/test_agent_mail.py, which mocks container_repo and
    # asserts the 401/400/429 paths end-to-end. The /addresses Clerk routes are
    # NOT excluded here — they go through the shared Clerk auth override.
    .exclude(
        path_regex="^/api/v1/agent-mail/u/",
    )
    # Container-authed company hook routes (hook token, same shape as
    # agent-mail): Hypothesis filters too many generated owner_id/body pairs
    # here (filter_too_much). Covered end-to-end by
    # tests/unit/routers/test_company_stripe.py and fuzzed for 5xx by
    # tests/chaos/api/test_contract_negative.py, which suppresses that check.
    .exclude(
        path_regex="^/api/v1/company/[a-z-]+/u/",
    )
    .exclude(
        path_regex="^/health$",
    )
)


@schema.parametrize()
# filter_too_much is a generation-EFFICIENCY check, not a correctness one: it
# errors when hypothesis discards most drafts before they reach the endpoint.
# The provisioning-workflow routes (deep nullable bodies behind a path param)
# trip it randomly, failing one route per run with nothing wrong. The run
# still exercises whatever examples survive; suppressing the check just stops
# the flake from failing the suite.
@settings(suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow])
def test_no_server_errors(case):
    """No endpoint should return 5xx for schema-valid requests.

    This catches unhandled exceptions, missing error handling, and
    crashes from unexpected input shapes. Only checks for server errors;
    does not validate response schemas or status code documentation.

    Excluded endpoints (require infrastructure):
    - WebSocket routes (/ws/*) - need API Gateway + DynamoDB
    - Webhook routes (/webhooks/*) - need Clerk signature verification
    - Agent write operations - need gateway for workspace management
    - Health check (/health) - needs DynamoDB connectivity
    - Users (/users/*) - needs DynamoDB user repo
    """
    case.call_and_validate(checks=[not_a_server_error])
