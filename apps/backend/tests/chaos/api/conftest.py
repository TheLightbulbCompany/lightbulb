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

import schemathesis

from main import app

# Built once; reused by the parametrized tests in this directory.
schema = schemathesis.openapi.from_asgi("/api/v1/openapi.json", app)

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
    # Webhook ingress that looks up a hook token then forwards to the container;
    # the hook-token table + container are absent hermetically.
    "POST /api/v1/hooks/u/{owner_id}/{hook_path}": "forwards to a live container; hook-token table not provisioned hermetically",
    # Pure-DDB read, but the seller-profile table-name env is not wired into the
    # chaos fixture → boto ParamValidationError (empty TableName). FOLLOW-UP:
    # wire the seller table into the fixture to fuzz this endpoint for real.
    "GET /api/v1/marketplace/seller/handle/{handle}": "seller-profile table env not wired in fixture (empty TableName); follow-up to provision it",
}
