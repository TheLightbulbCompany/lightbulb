"""
Schemathesis contract tests.

Auto-validates all API endpoints against the OpenAPI specification.
Uses ASGI transport so no running server is needed.

With mocked dependencies, endpoints that require external services
(gateway, DynamoDB, Clerk webhook verification) are excluded from automated
fuzz testing since they cannot be meaningfully tested without infrastructure.
"""

import schemathesis
from schemathesis.checks import not_a_server_error

from core.auth import AuthContext


def _make_app():
    """Create a FastAPI app with mocked dependencies for contract testing."""
    from main import app
    from core.auth import get_current_user

    mock_auth = AuthContext(user_id="contract_test_user")

    async def mock_get_current_user():
        return mock_auth

    app.dependency_overrides[get_current_user] = mock_get_current_user
    return app


app = _make_app()

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
    # OpenClaw control-ui proxy streams HTML from the user's container; the
    # contract harness has no gateway connection pool mocked, so fuzzing
    # reaches the real proxy code and crashes.
    .exclude(
        path_regex="^/api/v1/control-ui/",
    )
    # Desktop auth requires CLERK_SECRET_KEY (real Clerk credential) to mint
    # sign-in tokens via the upstream Clerk API. CI doesn't set the secret, so
    # the endpoint returns 500 by design ("CLERK_SECRET_KEY not configured").
    # This is consistent with /webhooks/* exclusion — both rely on real Clerk
    # infrastructure that the contract harness can't mock.
    .exclude(
        path_regex="^/api/v1/auth/",
    )
    # Marketplace endpoints have the same Stripe + DDB dependency profile
    # as /billing/ — checkout, refund, payouts/onboard reach the real
    # stripe SDK which 401s without a real STRIPE_SECRET_KEY, and listing
    # write/read paths chain into S3 (artifacts bucket) and the agent
    # gateway (artifact-from-agent). Marketplace unit tests under
    # tests/unit/services/test_payout_service.py + test_marketplace_*
    # cover these paths with proper mocks.
    .exclude(
        path_regex="^/api/v1/marketplace/",
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
    .exclude(
        path_regex="^/health$",
    )
)


@schema.parametrize()
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
