import os

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Isol8"
    API_V1_STR: str = "/api/v1"

    # Environment mode
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "")

    # Clerk Auth
    CLERK_ISSUER: str = os.getenv("CLERK_ISSUER", "https://your-clerk-domain.clerk.accounts.dev")
    CLERK_AUDIENCE: str | None = None

    # DynamoDB
    DYNAMODB_TABLE_PREFIX: str = os.getenv("DYNAMODB_TABLE_PREFIX", "isol8-dev-")
    DYNAMODB_ENDPOINT_URL: str | None = os.getenv("DYNAMODB_ENDPOINT_URL", None)

    # Clerk Secret Key (for fetching user/org metadata)
    CLERK_SECRET_KEY: str | None = os.getenv("CLERK_SECRET_KEY")

    # Clerk Webhook Secret (svix signing secret for verifying Clerk webhook payloads)
    CLERK_WEBHOOK_SECRET: str | None = os.getenv("CLERK_WEBHOOK_SECRET")

    # AWS Configuration
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
    BEDROCK_ENABLED: bool = os.getenv("BEDROCK_ENABLED", "true").lower() == "true"

    # CORS Configuration (comma-separated origins; deployed values set by Terraform)
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Public-facing base URL for this API. Used to build absolute URLs in
    # responses (e.g., the webhook URL surfaced via /hooks/credentials).
    # Per env: https://api-dev.isol8.co, https://api-staging.isol8.co,
    # https://api.isol8.co. Defaults to localhost for dev/test. Wired via
    # CDK service-stack environment block per environment.
    PUBLIC_API_BASE: str = os.getenv("PUBLIC_API_BASE", "http://localhost:8000")

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS as comma-separated list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    # --- ECS Fargate ---
    ECS_CLUSTER_ARN: str = os.getenv("ECS_CLUSTER_ARN", "")
    # ECS_TASK_DEFINITION removed — EcsManager now reads the latest CDK base
    # revision live via describe_task_definition(family) on each provision.
    # Per-user clones live in a separate family (`<base>-user`) so the base
    # family is uncontaminated. See #410.
    ECS_SUBNETS: str = os.getenv("ECS_SUBNETS", "")  # comma-separated
    ECS_SECURITY_GROUP_ID: str = os.getenv("ECS_SECURITY_GROUP_ID", "")

    # --- Cloud Map ---
    CLOUD_MAP_NAMESPACE_ID: str = os.getenv("CLOUD_MAP_NAMESPACE_ID", "")
    CLOUD_MAP_SERVICE_ID: str = os.getenv("CLOUD_MAP_SERVICE_ID", "")
    CLOUD_MAP_SERVICE_ARN: str = os.getenv("CLOUD_MAP_SERVICE_ARN", "")
    CLOUD_MAP_NAMESPACE_NAME: str = os.getenv("CLOUD_MAP_NAMESPACE_NAME", "")
    CLOUD_MAP_SERVICE_NAME: str = os.getenv("CLOUD_MAP_SERVICE_NAME", "")

    # --- EFS ---
    EFS_MOUNT_PATH: str = os.getenv("EFS_MOUNT_PATH", "/mnt/efs/users")
    EFS_FILE_SYSTEM_ID: str = os.getenv("EFS_FILE_SYSTEM_ID", "")

    # --- S3 Config ---
    S3_CONFIG_BUCKET: str = os.getenv("S3_CONFIG_BUCKET", "")

    # --- PostHog (server-side, distinct from NEXT_PUBLIC_POSTHOG_KEY) ---
    # Used by core/services/posthog_admin.py to query the Persons API for the
    # admin dashboard's Activity tab. Empty defaults so the admin client
    # stubs gracefully (returns {events: [], stubbed: true}) when unset —
    # local dev works without a real PostHog project.
    POSTHOG_HOST: str = os.getenv("POSTHOG_HOST", "https://app.posthog.com")
    POSTHOG_PROJECT_ID: str = os.getenv("POSTHOG_PROJECT_ID", "")
    POSTHOG_PROJECT_API_KEY: str = os.getenv("POSTHOG_PROJECT_API_KEY", "")

    # --- IAM ---
    CONTAINER_EXECUTION_ROLE_ARN: str = os.getenv("CONTAINER_EXECUTION_ROLE_ARN", "")

    # --- OpenClaw ---
    # The container image is pinned in openclaw-version.json at the repo root
    # and imported directly by the CDK container stack. No env var override
    # in production — bump the JSON file to upgrade.

    # Webhook event-id dedup table (Stripe + Clerk). Conditional PutItem with
    # attribute_not_exists drops replays at the edge. Wired via CDK service-stack.
    WEBHOOK_DEDUP_TABLE: str = ""

    # WebSocket Configuration (API Gateway Management API)
    WS_CONNECTIONS_TABLE: str = os.getenv("WS_CONNECTIONS_TABLE", "isol8-websocket-connections")
    WS_MANAGEMENT_API_URL: str = os.getenv("WS_MANAGEMENT_API_URL", "")  # Set by Terraform
    # Public WSS endpoint returned by POST /api/v1/desktop/pair so desktop
    # clients know which gateway to connect to. Wired by CDK service-stack from
    # api-stack's WebSocket stage URL.
    WS_GATEWAY_URL: str = os.getenv("WS_GATEWAY_URL", "wss://ws-dev.isol8.co")

    # Credit ledger + OAuth tables (set by CDK)
    CREDITS_TABLE: str = ""
    CREDIT_TRANSACTIONS_TABLE: str = ""
    LEDGER_BACKEND: str = "ddb"  # ledger system-of-record: "ddb" (interim) | "metronome" (Phase 3)
    METER_IS_LIVE: bool = False  # cutover flag OWNED by Plan 4: when True, Plan 3's AWS meter deplete() is the sole biller (and Plan 3 then calls enforce_after_deplete) and the legacy connection_pool deduct is gated off; when False, Plan 3 is shadow-only (logs, no deplete, no enforce) and the legacy deduct is the live biller. Both paths read this ONE flag so the cutover is order-independent.
    OAUTH_TOKENS_TABLE: str = ""

    # --- Metronome lifecycle + gating ---
    # Metronome is the usage ledger; the backend never ingests usage into it
    # (the meter Lambda does that directly). These settings serve the
    # backend's lifecycle/gating modules: customer provisioning, payment-
    # gated top-up funding, the low-balance webhook, and balance display.
    # The environment is token-scoped: same base URL, the token decides
    # sandbox vs prod, so a sandbox API key only ever touches sandbox data.
    # Empty defaults keep the backend booting in non-Metronome contexts
    # (unit tests, local dev). LEDGER_BACKEND stays "ddb" — the DDB ledger
    # remains the billed truth until it is deleted outright; there is no
    # config flip that makes the backend read balances from Metronome.
    METRONOME_BASE_URL: str = os.getenv("METRONOME_BASE_URL", "https://api.metronome.com/v1")
    """Metronome API base, including the /v1 prefix. Same URL for sandbox + prod."""
    METRONOME_API_KEY: str = os.getenv("METRONOME_API_KEY", "")
    """Bearer token. Sandbox token in dev, prod token in prod. From Secrets Manager."""
    METRONOME_WEBHOOK_SECRET: str = os.getenv("METRONOME_WEBHOOK_SECRET", "")
    """HMAC-SHA256 key for verifying Metronome-Webhook-Signature on inbound webhooks."""
    METRONOME_FAST_GATE_TABLE: str = os.getenv("METRONOME_FAST_GATE_TABLE", "")
    """DDB table backing the local fast-gate flag the synchronous chat gate reads."""
    METRONOME_LIFECYCLE_ENABLED: bool = os.getenv("METRONOME_LIFECYCLE_ENABLED", "false").lower() == "true"
    """Master switch for the Metronome customer lifecycle (provisioning,
    top-up funding, balance display). When False the ~3s /container/status
    poll never spams doomed calls while the API-key secrets are still
    placeholders. Flipped per env once the runbook (tokens + rate card +
    webhook secret) is done. Webhook HMAC verification is independent of
    this flag (fail-closed on an empty secret)."""
    METRONOME_RATE_CARD_ID: str = os.getenv("METRONOME_RATE_CARD_ID", "")
    """Rate card the per-customer contract references at provision time.
    Created once per env in the Metronome console (runbook step)."""
    METRONOME_CREDITS_PRODUCT_ID: str = os.getenv("METRONOME_CREDITS_PRODUCT_ID", "")
    """Metronome product id the prepaid credit commit draws against. Per-env."""
    METRONOME_LOW_BALANCE_THRESHOLD_CENTS: int = int(os.getenv("METRONOME_LOW_BALANCE_THRESHOLD_CENTS", "500"))
    """Threshold for the per-customer low_remaining_credit_balance_reached alert
    created at provision time. When an owner's prepaid balance crosses below
    this, Metronome fires the webhook that sets the fast-gate flag."""

    BEDROCK_ENFORCEMENT_LIVE: bool = os.getenv("BEDROCK_ENFORCEMENT_LIVE", "false").lower() == "true"
    """Arms gate + kill-switch enforcement off the Metronome low-balance
    webhook. When False (default) the webhook still sets the fast-gate flag —
    ingest is always-on shadow — but never fires the kill-switch, so the
    Metronome pipeline can be validated end-to-end with zero blast radius."""

    # Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). Empty defaults
    # keep the backend booting in non-AWS unit-test contexts.
    BEDROCK_AIP_OWNERS_TABLE: str = os.getenv("BEDROCK_AIP_OWNERS_TABLE", "")
    """Reverse index PK aip_arn -> owner_id. Read by resolve_owner_from_aip_arn."""
    BEDROCK_OWNER_ROLE_PREFIX: str = os.getenv("BEDROCK_OWNER_ROLE_PREFIX", "")
    """IAM role-name prefix for backend-minted per-owner roles, e.g.
    'isol8-dev-owner-bedrock'. Set by CDK; matches the PassRole/CreateRole scope."""
    BEDROCK_OWNER_BOUNDARY_ARN: str = os.getenv("BEDROCK_OWNER_BOUNDARY_ARN", "")
    """Permissions-boundary policy ARN every backend-minted role must carry."""
    BEDROCK_OWNER_BASE_POLICY_ARN: str = os.getenv("BEDROCK_OWNER_BASE_POLICY_ARN", "")
    """CDK-managed EFS/logs/ssm managed-policy ARN attached to every per-owner role."""
    BEDROCK_AIP_FAN_OUT_REGIONS: str = os.getenv("BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2")
    """Comma-separated regions the per-owner role permits the FM ARN in."""

    # Billing / Stripe
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_FLAT_PRICE_ID: str = ""

    # Marketplace (set by CDK ServiceStack — empty default lets the backend
    # boot in non-marketplace contexts like local unit tests).
    MARKETPLACE_LISTINGS_TABLE: str = os.getenv("MARKETPLACE_LISTINGS_TABLE", "")
    MARKETPLACE_PURCHASES_TABLE: str = os.getenv("MARKETPLACE_PURCHASES_TABLE", "")
    MARKETPLACE_PAYOUT_ACCOUNTS_TABLE: str = os.getenv("MARKETPLACE_PAYOUT_ACCOUNTS_TABLE", "")
    MARKETPLACE_TAKEDOWNS_TABLE: str = os.getenv("MARKETPLACE_TAKEDOWNS_TABLE", "")
    MARKETPLACE_SELLERS_TABLE: str = os.getenv("MARKETPLACE_SELLERS_TABLE", "")
    MARKETPLACE_ARTIFACTS_BUCKET: str = os.getenv("MARKETPLACE_ARTIFACTS_BUCKET", "")

    # Stripe Connect (seller payout onboarding) — environment-specific URLs
    # set by CDK so the marketplace UI can return the user back to the right
    # host after Stripe-hosted onboarding.
    STRIPE_CONNECT_REFRESH_URL: str = os.getenv("STRIPE_CONNECT_REFRESH_URL", "")
    STRIPE_CONNECT_RETURN_URL: str = os.getenv("STRIPE_CONNECT_RETURN_URL", "")

    # Stripe Connect webhook signing secret (separate from STRIPE_WEBHOOK_SECRET
    # used for the platform-account billing webhook). Verified by the
    # marketplace purchases router on POST /webhooks/stripe-marketplace.
    STRIPE_CONNECT_WEBHOOK_SECRET: str = os.getenv("STRIPE_CONNECT_WEBHOOK_SECRET", "")

    # Encryption (base64-encoded 32-byte key for Fernet encryption of BYOK API keys)
    ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")

    # Shared password for the gog CLI file-keyring backend. The gog_cli
    # materializer runs gog with GOG_KEYRING_BACKEND=file and this password to
    # encrypt the owner's Google OAuth keyring on EFS; the identical value is
    # injected into every per-user OpenClaw container so it can read the keyring
    # at runtime. Provisioned by CDK from the `isol8/{env}/gog-keyring-password`
    # secret — must stay stable forever or existing keyrings become unreadable.
    GOG_KEYRING_PASSWORD: str = os.getenv("GOG_KEYRING_PASSWORD", "")

    # KMS CMK ARN/alias for per-container secrets (operator device private keys,
    # gateway tokens). Provisioned by CDK as `alias/isol8-{env}-container-secrets`.
    # Backend calls kms:Encrypt at provision time and kms:Decrypt at handshake
    # time — both operations are audited via CloudTrail so we have a per-call
    # record of which backend instance touched which container's secrets.
    CONTAINER_SECRETS_KMS_KEY_ID: str = os.getenv("CONTAINER_SECRETS_KMS_KEY_ID", "")

    # --- Paperclip integration ---
    # Internal URL the backend uses to reach Paperclip's REST API (private subnet).
    # e.g. ``http://paperclip.internal.isol8.local`` — set by CDK in deployed envs.
    PAPERCLIP_INTERNAL_URL: str = os.getenv("PAPERCLIP_INTERNAL_URL", "")
    # Public-facing URL for browsers (used by the proxy router for cookie scope
    # and origin checks; T14/T15 introduce the actual proxy). e.g.
    # ``https://company.isol8.co``.
    PAPERCLIP_PUBLIC_URL: str = os.getenv("PAPERCLIP_PUBLIC_URL", "")
    # HMAC key for the OpenClaw service-token JWTs the backend mints for
    # seeded Paperclip agents (see core/services/service_token.py). Populated
    # from Secrets Manager + KMS by the CDK service stack at deploy time.
    # Re-used by paperclip_proxy to sign the proxy's own session cookie.
    PAPERCLIP_SERVICE_TOKEN_KEY: str = os.getenv("PAPERCLIP_SERVICE_TOKEN_KEY", "")
    # Clerk publishable key (the same value the frontend uses as
    # NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY). The paperclip-proxy bootstrap page
    # renders inline JS that loads the Clerk SDK and handshakes a session
    # cookie; the SDK needs this key to identify which Clerk instance to
    # talk to. Publishable keys are public by design — Clerk literally
    # serves them in HTML; storing in env (not Secrets Manager) is correct.
    CLERK_PUBLISHABLE_KEY: str = os.getenv("CLERK_PUBLISHABLE_KEY", "")
    # Public URL of the Isol8 frontend (e.g. https://dev.isol8.co). Used by
    # the paperclip-proxy to redirect users back to /chat when they land on
    # company.isol8.co without prerequisites (no container, no Clerk
    # session, no Paperclip company row). Same value as CORS_ORIGINS in
    # most envs but kept separate so the proxy doesn't need to parse a
    # comma-separated list.
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "https://dev.isol8.co")

    @field_validator("CLERK_ISSUER")
    @classmethod
    def validate_clerk_issuer(cls, v: str) -> str:
        if "your-clerk-domain" in v:
            raise ValueError("CLERK_ISSUER not configured. Set the CLERK_ISSUER environment variable.")
        return v

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
