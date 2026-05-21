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

    # --- Shipped-agent MCP credentials ---
    # JSON blob of third-party API keys forwarded into per-user OpenClaw
    # containers so that mcp.servers.<x>.env placeholders in shipped agents'
    # openclaw.json (Apollo, HubSpot, Stripe, Twilio, Perplexity, Attio,
    # Pipedrive, Instantly, SmartLead, PostHog, Qdrant, etc.) resolve at
    # runtime. CDK auth-stack defines the Secrets Manager entry; operator
    # populates with `aws secretsmanager update-secret`. Empty default keeps
    # provisioning safe — ecs_manager._build_mcp_environment_for_user returns
    # an empty list, MCPs spawn with no creds and 401 on tool calls instead
    # of blocking container boot.
    MCP_CREDENTIALS_JSON: str = os.getenv("MCP_CREDENTIALS_JSON", "")

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
    OAUTH_TOKENS_TABLE: str = ""

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
