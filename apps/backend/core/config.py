import os

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_V1_STR: str = "/api/v1"

    # Environment mode
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
    CLOUD_MAP_SERVICE_ARN: str = os.getenv("CLOUD_MAP_SERVICE_ARN", "")
    CLOUD_MAP_NAMESPACE_NAME: str = os.getenv("CLOUD_MAP_NAMESPACE_NAME", "")
    CLOUD_MAP_SERVICE_NAME: str = os.getenv("CLOUD_MAP_SERVICE_NAME", "")

    # --- EFS ---
    EFS_MOUNT_PATH: str = os.getenv("EFS_MOUNT_PATH", "/mnt/efs/users")
    EFS_FILE_SYSTEM_ID: str = os.getenv("EFS_FILE_SYSTEM_ID", "")

    # Public S3 URL of the hosted AWS read-only connector CloudFormation
    # template (the public-read isol8-{env}-cfn-templates bucket, wired by CDK
    # service-stack). The connections aws-setup endpoint feeds this as the
    # quick-create TemplateURL so the "Create role in AWS" button works.
    # CloudFormation requires an S3 URL it can fetch anonymously. Empty default
    # keeps the button disabled (and the backend booting) in non-AWS contexts.
    CFN_TEMPLATE_URL: str = os.getenv("CFN_TEMPLATE_URL", "")

    # --- PostHog (server-side, distinct from NEXT_PUBLIC_POSTHOG_KEY) ---
    # Used by core/services/posthog_admin.py to query the Persons API for the
    # admin dashboard's Activity tab. Empty defaults so the admin client
    # stubs gracefully (returns {events: [], stubbed: true}) when unset —
    # local dev works without a real PostHog project.
    POSTHOG_HOST: str = os.getenv("POSTHOG_HOST", "https://app.posthog.com")
    POSTHOG_PROJECT_ID: str = os.getenv("POSTHOG_PROJECT_ID", "")
    POSTHOG_PROJECT_API_KEY: str = os.getenv("POSTHOG_PROJECT_API_KEY", "")
    POSTHOG_PROJECT_WRITE_KEY: str = os.getenv("POSTHOG_PROJECT_WRITE_KEY", "")
    """PostHog project (``phc_``) INGESTION key for SERVER-SIDE capture —
    distinct from POSTHOG_PROJECT_API_KEY above (a personal ``phx_`` key used
    only for the read/query Persons API in posthog_admin.py). Used by
    core/observability/product_analytics.py to land billing-lifecycle events
    onto the person. This is the SAME public key the frontend ships as
    NEXT_PUBLIC_POSTHOG_KEY. Empty disables the bridge (events stay EMF-only)."""

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

    # Shared secret that authenticates the CALLER of the /api/v1/ws/* endpoints.
    # Those endpoints derive identity from request headers (x-user-id etc.) and
    # are meant to be invoked only by the trusted WebSocket Lambdas, which run
    # after the API Gateway WS authorizer validates the Clerk JWT. The public
    # HTTP API Gateway $default route, however, proxies every path to the same
    # ALB with no authorizer — so without this guard anyone can POST to
    # /api/v1/ws/connect with a forged x-user-id. The Lambdas attach this secret
    # as a header the public route cannot supply.
    #
    # Two flags so the mechanism can be rolled out with zero downtime: ship the
    # secret to both the Lambdas (which always attach it) and the backend, deploy,
    # confirm the header is arriving, then flip enforcement on. Enforcement is a
    # no-op until WS_ENFORCE_INTERNAL_SECRET is true AND a secret is configured.
    WS_INTERNAL_SECRET: str = os.getenv("WS_INTERNAL_SECRET", "")
    WS_ENFORCE_INTERNAL_SECRET: bool = os.getenv("WS_ENFORCE_INTERNAL_SECRET", "false").lower() == "true"

    # OAuth tables (set by CDK)
    OAUTH_TOKENS_TABLE: str = ""

    # --- Metronome lifecycle + gating ---
    # Metronome is the ONLY billing ledger; the backend never ingests usage
    # into it (the meter Lambda does that directly). These settings serve
    # the backend's lifecycle/gating modules: customer provisioning,
    # payment-gated top-up funding, the low-balance webhook, the gate's
    # lazy balance refresh, and balance display. The environment is
    # token-scoped: same base URL, the token decides sandbox vs prod, so a
    # sandbox API key only ever touches sandbox data. Empty defaults keep
    # the backend booting in non-Metronome contexts (unit tests, local dev).
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
    ACP_ENABLED: bool = os.getenv("ACP_ENABLED", "false").lower() == "true"
    """Master switch for the ACP coding-harness runtime (acpx). When True,
    write_openclaw_config emits the acp block + plugins.entries.acpx so a
    native agent (e.g. Jarvis) can sessions_spawn Claude Code / Codex / Gemini,
    with allowedAgents DERIVED from the owner's provider_choice. Dark by
    default: keep False until the acpx-baked extended image (Dockerfile
    Layer 12) is promoted in openclaw-version.json for the env — enabling it
    against an image without /app/dist/extensions/acpx makes every ACP spawn
    fail with 'ACP runtime backend is not configured'."""
    METRONOME_CREDITS_PRODUCT_ID: str = os.getenv("METRONOME_CREDITS_PRODUCT_ID", "")
    """Metronome product id the prepaid credit commit draws against. Per-env."""
    METRONOME_USD_CENTS_CREDIT_TYPE_ID: str = "2714e483-4ff1-48e4-9e25-ac732e8f24f2"
    """Metronome's global USD-(cents) credit-type id — identical in our
    sandbox and in Metronome's own docs example, hence a constant default
    rather than per-env wiring. The cents denomination is sandbox-confirmed
    (a 2000-cent commit reads back as getNetBalance 2000). Required on the
    low-balance alert create (the validator rejects commit-balance alert
    types without a credit_type_id) and on commit access schedules."""
    METRONOME_LOW_BALANCE_THRESHOLD_CENTS: int = int(os.getenv("METRONOME_LOW_BALANCE_THRESHOLD_CENTS", "500"))
    """Threshold for the per-customer low-remaining-commit-balance alert
    created at provision time. When an owner's prepaid balance crosses below
    this, Metronome fires the webhook that sets the fast-gate flag."""
    METRONOME_GATE_STALENESS_S: int = int(os.getenv("METRONOME_GATE_STALENESS_S", "60"))
    """Max age of the fast-gate row before a gate read lazily refreshes it
    from getNetBalance. 60s bounds the worst-case overrun past zero to about
    one minute of burn plus the metering pipeline's own ~1min lag — the cost
    is one balance call per ACTIVELY CHATTING owner per window, which is
    negligible; the -zero alert webhook remains the fast path. The refresh is
    also the pull-based re-arm path: a Metronome payment-gated top-up
    releases credits with NO webhook to us,
    so the first gate read after this window sees the new balance and
    un-revokes a paid-up owner (see core/services/metronome_gate.py)."""

    TRIAL_WAKE_GATE_ENABLED: bool = os.getenv("TRIAL_WAKE_GATE_ENABLED", "true").lower() == "true"
    """Kill switch for wake-on-intent gating of parked (stop_reason=trial_idle)
    trial containers. Off = every wake_policy entry point returns ALLOW/no-op,
    restoring wake-on-read exactly as before; stop_reason stamping and the
    chat-send server wake stay live either way (harmless, keeps re-enable
    coherent). Flipping requires a CDK deploy (~15-25 min) — but because the
    suppress marker is a query param (no CORS surface), a plain backend code
    revert is equally safe."""

    TRIAL_WAKE_DAILY_BUDGET: int = int(os.getenv("TRIAL_WAKE_DAILY_BUDGET", "6"))
    """Max legacy (un-flagged) poll-driven wakes of a parked trial container
    per UTC day. Bounds already-open tabs running pre-gate frontend bundles —
    the only lever that reaches them, since a never-reloaded tab keeps its old
    JS for weeks. Explicit intent (wake verb, chat send, retry) is never
    budgeted."""

    BEDROCK_ENFORCEMENT_LIVE: bool = os.getenv("BEDROCK_ENFORCEMENT_LIVE", "true").lower() == "true"
    """Arms the revoke-only enforcement off the Metronome low-balance
    webhook. Defaults True now that Metronome is the only ledger — there is
    no other biller to fall back to, so a depleted balance must revoke the
    owner's Bedrock IAM grant. Set "false" per env only to run the pipeline
    in shadow (flag set, no revoke) during validation."""

    # Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). Empty defaults
    # keep the backend booting in non-AWS unit-test contexts.
    BEDROCK_AIP_OWNERS_TABLE: str = os.getenv("BEDROCK_AIP_OWNERS_TABLE", "")
    """Reverse index PK aip_arn -> owner_id. Read by resolve_owner_from_aip_arn."""
    BEDROCK_OWNER_BOUNDARY_ARN: str = os.getenv("BEDROCK_OWNER_BOUNDARY_ARN", "")
    """Permissions-boundary policy ARN every backend-minted role must carry."""
    BEDROCK_OWNER_BASE_POLICY_ARN: str = os.getenv("BEDROCK_OWNER_BASE_POLICY_ARN", "")
    """CDK-managed EFS/logs/ssm managed-policy ARN attached to every per-owner role."""
    BEDROCK_AIP_FAN_OUT_REGIONS: str = os.getenv("BEDROCK_AIP_FAN_OUT_REGIONS", "us-east-1,us-east-2,us-west-2")
    """Comma-separated regions the per-owner role permits the FM ARN in."""
    BEDROCK_GATE_URL: str = os.getenv("BEDROCK_GATE_URL", "")
    """OVERRIDE for the bedrock-gate Function URL (tests / local dev only).

    Production resolution is RUNTIME discovery from SSM
    (``/isol8/{env}/bedrock-gate-url``, published by the bedrock-gate CDK
    stack) via ``core.billing.bedrock_gate_url.gate_url`` — the gateway is the
    STANDARD Bedrock path, so nothing is wired through task env. When this
    override is set it wins over SSM; empty (the default) means "ask SSM"."""

    # Billing / Stripe
    STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    STRIPE_FLAT_PRICE_ID: str = ""
    # Per-seat Stripe price for the Teams (org) subscription. Empty default so
    # the app boots without it; CDK-wired in dev/prod. Quantity = org member
    # count, no trial. This is the billing source of truth for "$20/seat".
    STRIPE_TEAM_SEAT_PRICE_ID: str = ""
    # Display dollars for one seat, echoed to the frontend via /billing/account
    # so the paywall renders "N × $20" without hardcoding the number in the UI.
    # Kept in lockstep with STRIPE_TEAM_SEAT_PRICE_ID's unit amount in Stripe.
    TEAM_SEAT_PRICE_USD: int = 20

    # Marketplace (set by CDK ServiceStack — empty default lets the backend
    # boot in non-marketplace contexts like local unit tests).
    MARKETPLACE_LISTINGS_TABLE: str = os.getenv("MARKETPLACE_LISTINGS_TABLE", "")
    MARKETPLACE_PURCHASES_TABLE: str = os.getenv("MARKETPLACE_PURCHASES_TABLE", "")
    MARKETPLACE_PAYOUT_ACCOUNTS_TABLE: str = os.getenv("MARKETPLACE_PAYOUT_ACCOUNTS_TABLE", "")
    MARKETPLACE_TAKEDOWNS_TABLE: str = os.getenv("MARKETPLACE_TAKEDOWNS_TABLE", "")
    MARKETPLACE_SELLERS_TABLE: str = os.getenv("MARKETPLACE_SELLERS_TABLE", "")
    MARKETPLACE_COMPANY_DEPLOYS_TABLE: str = os.getenv("MARKETPLACE_COMPANY_DEPLOYS_TABLE", "")
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
    # --- Platform Ops (operator console + owner-scoped ops API) ---
    # The founder's owner_id (user_* or org_*). The platform_ops token's
    # `sub` must equal this; it gates the agent path on /ops/*.
    PLATFORM_OPS_OWNER_ID: str = os.getenv("PLATFORM_OPS_OWNER_ID", "")
    # Dedicated HS256 key for signing/verifying platform_ops tokens. Distinct
    # from PAPERCLIP_SERVICE_TOKEN_KEY so ops tokens are independently
    # revocable. Populated from Secrets Manager + KMS by the CDK service stack.
    PLATFORM_OPS_TOKEN_KEY: str = os.getenv("PLATFORM_OPS_TOKEN_KEY", "")
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

    # --- Transactional email (Resend) ---
    # Resend is used for OUTBOUND app-generated mail only: the welcome email
    # on signup and (Plan 2) delivery of support tickets into the support@
    # inbox. The inbound support mailbox itself is Google Workspace, read by
    # the founder's agent — NOT Resend.
    #
    # Sending is gated solely on a real key being present (email_service skips
    # unless RESEND_API_KEY is re_-prefixed), so the CDK placeholder secret is a
    # no-op and the feature self-arms the moment a real key is deployed. No
    # separate enable flag.
    RESEND_API_KEY: str = os.getenv("RESEND_API_KEY", "")
    # Visible From identities on the verified isol8.co domain. Same across
    # envs, so plain defaults (no CDK wiring) — override via env only if a
    # future env needs a different sender.
    EMAIL_FROM_WELCOME: str = os.getenv("EMAIL_FROM_WELCOME", "Isol8 <hello@isol8.co>")
    # The Google Workspace support mailbox. Used as Reply-To on the welcome
    # email (a plain reply lands in the inbox the agent watches) and (Plan 2)
    # as the ticket destination.
    EMAIL_SUPPORT_ADDRESS: str = os.getenv("EMAIL_SUPPORT_ADDRESS", "support@isol8.co")
    # Sender identity for support-ticket delivery emails. Lands in the
    # EMAIL_SUPPORT_ADDRESS inbox; distinct from welcome's hello@ so a Gmail
    # filter / the agent can tell app-generated tickets from direct customer mail.
    EMAIL_FROM_TICKETS: str = os.getenv("EMAIL_FROM_TICKETS", "Isol8 Support <tickets@isol8.co>")

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
