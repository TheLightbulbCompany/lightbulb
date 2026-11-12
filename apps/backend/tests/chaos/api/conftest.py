"""SP3 API contract fuzzing — schemathesis schema built from the ASGI app.

The schema is built once at import time from the OpenAPI document the app
serves. Tests parametrized over it run in-process over ASGI (no live server),
and request the inherited ``chaos_ddb`` fixture (from tests/chaos/conftest.py)
so any DynamoDB-backed endpoint is moto-mocked rather than hitting real AWS.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "wHc3hAOcLlFzWyu3Ph7xIyClIdVQTrIzFOZDtu_pIEY=")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import pytest
import schemathesis

from main import app


# Built once; reused by the parametrized tests in this directory.
schema = schemathesis.openapi.from_asgi("/api/v1/openapi.json", app)


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch):
    """The negative fuzz runs signed out. Remove any get_current_user override a
    test that ran earlier in this process left on the shared app; the authed slice
    sets its own afterwards."""
    from core.auth import get_current_user

    monkeypatch.delitem(app.dependency_overrides, get_current_user, raising=False)


# Operations that 5xx purely from an unmocked external dependency even when
# unauthenticated (not a product bug) — excluded with a written reason.
# Keyed on the stable operation label ("METHOD /path") because schemathesis
# mutates case.method during fuzzing, so the generated method is unreliable.
# Populated empirically during implementation; NO silent skips.
NEGATIVE_PASS_EXCLUSIONS: dict[str, str] = {
    # Proxy to the per-user OpenClaw container after a ws-connections lookup;
    # 500 here is the connections table absent from the hermetic fixture +
    # no live container to proxy to — not a product bug.
    "POST /api/v1/ws/message": "proxies to a live container; ws-connections table not provisioned hermetically",
    # Same class: looks up the ws-connections table (absent hermetically) →
    # ConnectionServiceError 5xx on inputs that reach the lookup. Generation-
    # dependent, so it only surfaces on some seeds — exclude deterministically.
    "POST /api/v1/ws/connect": "ws-connections table not provisioned hermetically (connection lookup 5xx)",
    # Exchanges the OAuth code with the external provider after an oauth-state
    # lookup; both the state table and the provider call are absent hermetically.
    "GET /api/v1/oauth/{provider}/callback": "external OAuth code exchange + oauth-state table not provisioned hermetically",
    # Agent-started integration connect: same hook-token lookup on the DDB
    # containers table before anything else can run. Covered by
    # tests/unit/routers/test_connections.py::TestAuthorizeFromContainer
    # (401 / 409 / mint paths).
    "GET /api/v1/connections/u/{owner_id}/{provider}/authorize": "hook-token lookup; containers table not provisioned hermetically",
    # Agent-mail container routes: hook-token lookup on the DDB containers
    # table before anything else can run → unmocked GetItem 5xx. Covered by
    # tests/unit/routers/test_agent_mail.py.
    "POST /api/v1/agent-mail/u/{owner_id}/send": "hook-token lookup; containers table not provisioned hermetically",
    "POST /api/v1/agent-mail/u/{owner_id}/address": "hook-token lookup; containers table not provisioned hermetically",
    "GET /api/v1/agent-mail/u/{owner_id}/inbox": "hook-token lookup; containers table not provisioned hermetically",
}


@pytest.fixture(autouse=True)
def _meta_webhook_configured(monkeypatch):
    """Configure the Meta app-level webhook for the fuzz.

    Unconfigured, that route 503s wholesale — a deliberate fail-closed (an
    environment that can't verify a Meta signature must not be able to complete
    the subscribe handshake), but a 5xx the not_a_server_error check reads as a
    crash. Setting the verify token makes the fuzz exercise what actually
    guards the route: the 403 on a wrong verify token and the 401 on an
    unsigned delivery.
    """
    from core.config import settings

    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "chaos-verify-token")


@pytest.fixture(autouse=True)
def _messaging_table(chaos_ddb, monkeypatch):
    """Provision the messaging table for the fuzz.

    The /public/session-links/* and /public/session-copies/redeem routes take no
    sign-in, so they are the only messaging routes an unauthenticated request
    drives all the way to the store (every other one answers 401 first). With
    MESSAGING_TABLE unset the repo raises and the fuzz reads a harness gap as a
    500; with an empty table every generated token or code misses and the route
    answers its real 404.
    """
    from core.config import settings

    chaos_ddb.create_table(
        TableName="chaos-messaging",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(settings, "MESSAGING_TABLE", "chaos-messaging")
