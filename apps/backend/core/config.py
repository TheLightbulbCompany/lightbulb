import logging
import os
from urllib.parse import urlparse

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# ENVIRONMENT values that mean "this is a real, internet-reachable deployment"
# (vs. "local" / "test" / unset for local dev + CI). Deployed envs must have the
# security fences configured — see _validate_deployed_security below.
_DEPLOYED_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
_KNOWN_ENVIRONMENTS = _DEPLOYED_ENVIRONMENTS | frozenset({"local", "test"})


def apex_domain(host: str) -> str:
    """Registrable apex of ``host`` — its last two labels.

    ``dev.isol8.co`` -> ``isol8.co``; ``isol8.co`` -> ``isol8.co``; ``""`` -> ``""``.
    Every product surface is a subdomain of ONE apex, so this is what a cookie
    scope, a same-site allowlist or a SES parent identity must key on.
    """
    # ponytail: two labels. A multi-label public suffix (``example.co.uk``)
    # would need the real PSL — no product domain has ever been one.
    return ".".join((host or "").strip().lower().strip(".").split(".")[-2:])


def url_host(url: str) -> str:
    """Hostname of ``url``, lowercased; ``""`` when it has none."""
    return (urlparse(url or "").hostname or "").lower()


class Settings(BaseSettings):
    """Backend settings.

    Every field is populated from the environment variable of the SAME NAME —
    that is ``BaseSettings``' own behaviour. The value right of each ``=`` is
    only the fallback used when the variable is unset.

    Do not reintroduce ``os.getenv("FIELD", default)`` as a default. It reads
    like it is doing the env lookup, but it never was: pydantic's own env
    source outranks a field default, so whenever the variable is set the
    ``os.getenv`` result is computed and then thrown away. All it can ever
    contribute is the fallback the literal now supplies directly. Same for
    hand-parsing (``.lower() == "true"``, ``int(...)``) — pydantic coerces the
    env string to the annotated type, which is what actually produced every
    bool and int here.
    """

    API_V1_STR: str = "/api/v1"

    # Environment mode
    ENVIRONMENT: str = ""

    # Clerk Auth
    CLERK_ISSUER: str = "https://your-clerk-domain.clerk.accounts.dev"
    # SEC-8 token-origin fence. Clerk session tokens carry NO `aud` claim (all
    # our clients call plain getToken(), no JWT template), so the origin check
    # is the `azp` (authorized party) claim — the frontend origin that minted
    # the token. Comma-separated allowlist of origins whose tokens this API
    # accepts; empty disables the check. Native-app tokens (mobile) carry no
    # `azp` and always pass, per Clerk's own SDK semantics.
    CLERK_AUTHORIZED_PARTIES: str = ""
    # "audit" → log + metric on mismatch, never reject; "enforce" → reject.
    # Rollout: deploy in audit, confirm zero mismatches in CloudWatch, flip.
    CLERK_AZP_MODE: str = "audit"

    # DynamoDB
    DYNAMODB_TABLE_PREFIX: str = "lightbulb-dev-"
    DYNAMODB_ENDPOINT_URL: str | None = None

    # Clerk Secret Key (for fetching user/org metadata)
    CLERK_SECRET_KEY: str | None = None

    # Clerk Webhook Secret (svix signing secret for verifying Clerk webhook payloads)
    CLERK_WEBHOOK_SECRET: str | None = None

    # AWS Configuration
    AWS_REGION: str = "us-east-1"

    # CORS Configuration (comma-separated origins; deployed values set by Terraform)
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Public-facing base URL for this API. Used to build absolute URLs in
    # responses (e.g., the webhook URL surfaced via /hooks/credentials).
    # Per env: https://api-dev.flickmeon.ai, https://api.flickmeon.ai,
    # https://api.isol8.co. Defaults to localhost for dev/test. Wired via
    # CDK service-stack environment block per environment.
    PUBLIC_API_BASE: str = "http://localhost:8000"

    @property
    def frontend_host(self) -> str:
        """Hostname FRONTEND_URL is served from (e.g. ``dev.isol8.co``)."""
        return url_host(self.FRONTEND_URL)

    @property
    def root_domain(self) -> str:
        """Product apex domain (e.g. ``isol8.co``) — derived from FRONTEND_URL.

        The cookie scope, the same-site return-URL allowlist and the admin host
        all hang off this, so a domain move is a FRONTEND_URL change, not a
        code change.
        """
        return apex_domain(self.frontend_host)

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS as comma-separated list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def clerk_authorized_parties_list(self) -> list[str]:
        """Parse CLERK_AUTHORIZED_PARTIES as comma-separated list."""
        return [p.strip() for p in self.CLERK_AUTHORIZED_PARTIES.split(",") if p.strip()]

    # --- ECS Fargate ---
    ECS_CLUSTER_ARN: str = ""
    # ECS_TASK_DEFINITION removed — EcsManager now reads the latest CDK base
    # revision live via describe_task_definition(family) on each provision.
    # Per-user clones live in a separate family (`<base>-user`) so the base
    # family is uncontaminated. See #410.
    ECS_SUBNETS: str = ""  # comma-separated
    ECS_SECURITY_GROUP_ID: str = ""

    # PROVISION Step Functions state machine ARN — THE provisioning executor
    # (spec §7, PR-10; no flag, active on merge). Dispatched by
    # core.services.provision_dispatch.dispatch_provision via StartExecution;
    # the one-shot scripts/provision_runner.py runs as an ECS task the
    # machine supervises, never in-process in this backend.
    PROVISION_STATE_MACHINE_ARN: str = ""

    # --- Cloud Map ---
    CLOUD_MAP_SERVICE_ARN: str = ""
    CLOUD_MAP_NAMESPACE_NAME: str = ""
    CLOUD_MAP_SERVICE_NAME: str = ""

    # --- EFS ---
    EFS_MOUNT_PATH: str = "/mnt/efs/users"
    EFS_FILE_SYSTEM_ID: str = ""

    # Public S3 URL of the hosted AWS read-only connector CloudFormation
    # template (the public-read isol8-{env}-cfn-templates bucket, wired by CDK
    # service-stack). The connections aws-setup endpoint feeds this as the
    # quick-create TemplateURL so the "Create role in AWS" button works.
    # CloudFormation requires an S3 URL it can fetch anonymously. Empty default
    # keeps the button disabled (and the backend booting) in non-AWS contexts.
    CFN_TEMPLATE_URL: str = ""

    # --- PostHog (server-side, distinct from NEXT_PUBLIC_POSTHOG_KEY) ---
    # Used by core/services/posthog_admin.py to query the Persons API for the
    # admin dashboard's Activity tab. Empty defaults so the admin client
    # stubs gracefully (returns {events: [], stubbed: true}) when unset —
    # local dev works without a real PostHog project.
    POSTHOG_HOST: str = "https://app.posthog.com"
    POSTHOG_PROJECT_ID: str = ""
    POSTHOG_PROJECT_API_KEY: str = ""
    POSTHOG_PROJECT_WRITE_KEY: str = ""
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
    WS_CONNECTIONS_TABLE: str = "lightbulb-dev-ws-connections"
    WS_MANAGEMENT_API_URL: str = ""  # Set by Terraform
    # Public WSS endpoint returned by POST /api/v1/desktop/pair so desktop
    # clients know which gateway to connect to. Wired by CDK service-stack from
    # api-stack's WebSocket stage URL.
    WS_GATEWAY_URL: str = "wss://ws-dev.flickmeon.ai"

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
    WS_INTERNAL_SECRET: str = ""
    WS_ENFORCE_INTERNAL_SECRET: bool = False

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
    METRONOME_BASE_URL: str = "https://api.metronome.com/v1"
    """Metronome API base, including the /v1 prefix. Same URL for sandbox + prod."""
    METRONOME_API_KEY: str = ""
    """Bearer token. Sandbox token in dev, prod token in prod. From Secrets Manager."""
    METRONOME_WEBHOOK_SECRET: str = ""
    """HMAC-SHA256 key for verifying Metronome-Webhook-Signature on inbound webhooks."""
    METRONOME_FAST_GATE_TABLE: str = ""
    """DDB table backing the local fast-gate flag the synchronous chat gate reads."""
    METRONOME_RATE_CARD_ID: str = ""
    """Rate card the per-customer contract references at provision time.
    Created once per env in the Metronome console (runbook step)."""
    METRONOME_CREDITS_PRODUCT_ID: str = ""
    """Metronome product id the prepaid credit commit draws against. Per-env."""
    METRONOME_USD_CENTS_CREDIT_TYPE_ID: str = "2714e483-4ff1-48e4-9e25-ac732e8f24f2"
    """Metronome's global USD-(cents) credit-type id — identical in our
    sandbox and in Metronome's own docs example, hence a constant default
    rather than per-env wiring. The cents denomination is sandbox-confirmed
    (a 2000-cent commit reads back as getNetBalance 2000). Required on the
    low-balance alert create (the validator rejects commit-balance alert
    types without a credit_type_id) and on commit access schedules."""
    METRONOME_LOW_BALANCE_THRESHOLD_CENTS: int = 500
    """Threshold for the per-customer low-remaining-commit-balance alert
    created at provision time. When an owner's prepaid balance crosses below
    this, Metronome fires the webhook that sets the fast-gate flag."""
    METRONOME_GATE_STALENESS_S: int = 60
    """Max age of the fast-gate row before a gate read lazily refreshes it
    from getNetBalance. 60s bounds the worst-case overrun past zero to about
    one minute of burn plus the metering pipeline's own ~1min lag — the cost
    is one balance call per ACTIVELY CHATTING owner per window, which is
    negligible; the -zero alert webhook remains the fast path. The refresh is
    also the pull-based re-arm path: a Metronome payment-gated top-up
    releases credits with NO webhook to us,
    so the first gate read after this window sees the new balance and
    un-revokes a paid-up owner (see core/services/metronome_gate.py)."""

    TRIAL_WAKE_GATE_ENABLED: bool = True
    """Kill switch for wake-on-intent gating of parked (stop_reason=trial_idle)
    trial containers. Off = every wake_policy entry point returns ALLOW/no-op,
    restoring wake-on-read exactly as before; stop_reason stamping and the
    chat-send server wake stay live either way (harmless, keeps re-enable
    coherent). Flipping requires a CDK deploy (~15-25 min) — but because the
    suppress marker is a query param (no CORS surface), a plain backend code
    revert is equally safe."""

    TRIAL_WAKE_DAILY_BUDGET: int = 6
    """Max legacy (un-flagged) poll-driven wakes of a parked trial container
    per UTC day. Bounds already-open tabs running pre-gate frontend bundles —
    the only lever that reaches them, since a never-reloaded tab keeps its old
    JS for weeks. Explicit intent (wake verb, chat send, retry) is never
    budgeted."""

    BROWSER_EGRESS_PROXY_PORT: int = 0
    """Loopback port of the container's Cloudflare WARP local proxy, or 0 for
    off. Non-zero makes write_openclaw_config give the browser tool's Chrome a
    PAC pointing at 127.0.0.1:<port>, so page loads egress on a Cloudflare IP
    instead of the AWS NAT IP (the thing that trips datacenter-IP CAPTCHA
    walls). Nothing else in the container is affected: a loopback proxy reaches
    only processes explicitly pointed at it, and this is the only pointer.

    The PAC ends in "; DIRECT", so a container whose warp-svc never connected
    falls back to today's egress instead of losing the browser (measured: 0.6s
    to first byte on the fallback). That makes this knob safe to flip ahead of
    the container side being healthy, though the useful order is the reverse —
    see the comment on the CDK env var in service-stack.ts.

    Container-side counterpart: the same port is set on the per-user task
    definition and written into WARP's mdm.xml as proxy_port. Both come from
    one constant, BROWSER_EGRESS_PROXY_PORT in container-stack.ts."""

    BEDROCK_ENFORCEMENT_LIVE: bool = True
    """Arms the revoke-only enforcement off the Metronome low-balance
    webhook. Defaults True now that Metronome is the only ledger — there is
    no other biller to fall back to, so a depleted balance must revoke the
    owner's Bedrock IAM grant. Set "false" per env only to run the pipeline
    in shadow (flag set, no revoke) during validation."""

    # Per-owner Bedrock AIP + scoped-role enforcement (Plan 2). Empty defaults
    # keep the backend booting in non-AWS unit-test contexts.
    BEDROCK_AIP_OWNERS_TABLE: str = ""
    """Reverse index PK aip_arn -> owner_id. Read by resolve_owner_from_aip_arn."""
    BEDROCK_OWNER_BOUNDARY_ARN: str = ""
    """Permissions-boundary policy ARN every backend-minted role must carry."""
    BEDROCK_OWNER_BASE_POLICY_ARN: str = ""
    """CDK-managed EFS/logs/ssm managed-policy ARN attached to every per-owner role."""
    BEDROCK_AIP_FAN_OUT_REGIONS: str = "us-east-1,us-east-2,us-west-2"
    """Comma-separated regions the per-owner role permits the FM ARN in."""
    BEDROCK_GATE_URL: str = ""
    """OVERRIDE for the bedrock-gate Function URL (tests / local dev only).

    Production resolution is RUNTIME discovery from SSM
    (``/isol8/{env}/bedrock-gate-url``, published by the bedrock-gate CDK
    stack) via ``core.billing.bedrock_gate_url.gate_url`` — the gateway is the
    STANDARD Bedrock path, so nothing is wired through task env. When this
    override is set it wins over SSM; empty (the default) means "ask SSM"."""

    # Billing / Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_FLAT_PRICE_ID: str = ""
    # Per-seat Stripe price for the Teams (org) subscription. Empty default so
    # the app boots without it; CDK-wired in dev/prod. Quantity = org member
    # count, no trial. This is the billing source of truth for "$20/seat".
    STRIPE_TEAM_SEAT_PRICE_ID: str = ""
    # Flat monthly base for the Teams (org) subscription, charged once
    # alongside the per-seat item. Empty default so the app boots without it;
    # CDK-wired in dev/prod. This is the billing source of truth for "$50 base".
    STRIPE_TEAM_BASE_PRICE_ID: str = ""
    # Stripe price for the Ultra Pro tier (large marketplace companies exceeding
    # the flat tier's agent budget). Empty default so the app boots without it;
    # CDK-wired in dev/prod. This is the billing source of truth for "$150".
    STRIPE_ULTRA_PRICE_ID: str = ""
    # Display dollars for one seat, echoed to the frontend via /billing/account
    # so the paywall renders "N × $20" without hardcoding the number in the UI.
    # Kept in lockstep with STRIPE_TEAM_SEAT_PRICE_ID's unit amount in Stripe.
    TEAM_SEAT_PRICE_USD: int = 20
    # Display dollars for the Teams base, echoed to the frontend via
    # /billing/account so the paywall renders "$50 + N × $20" without
    # hardcoding the number in the UI. Kept in lockstep with
    # STRIPE_TEAM_BASE_PRICE_ID's unit amount in Stripe.
    TEAM_BASE_PRICE_USD: int = 50
    # Display dollars for the personal flat plan, echoed to clients via
    # /billing/account. Kept in lockstep with STRIPE_FLAT_PRICE_ID's unit
    # amount in Stripe. The iOS paywall renders the web price from this, and
    # App Review requires a displayed external price to be accurate, so a
    # divergence here is a compliance bug rather than a cosmetic one.
    FLAT_PRICE_USD: int = 50

    # Apple In-App Purchase (App Store guideline 3.1.3(b) — the plan must also
    # be buyable inside the iOS app). ALL default empty, and the feature is off
    # while APPLE_IAP_BUNDLE_ID is: the Paid Apps Agreement has to be active
    # before a subscription product can exist, so every environment boots
    # without these and the router answers 503 rather than 500.
    APPLE_IAP_BUNDLE_ID: str = ""
    # Numeric App Store app id (6788944100). Optional — the verifier uses it to
    # bind notifications to this app when present.
    APPLE_IAP_APP_APPLE_ID: str = ""
    # "sandbox" (default) or "production". A sandbox transaction fails
    # verification against a production verifier and vice versa, so this MUST
    # match the build being served: TestFlight and the simulator are sandbox.
    APPLE_IAP_ENVIRONMENT: str = "sandbox"
    # Directory holding Apple's root .cer files. The certificate chain is
    # pinned to these, so an empty or wrong directory disables the feature
    # rather than weakening it — see apple_iap._verifier.
    APPLE_IAP_ROOT_CERTS_DIR: str = ""
    # The one product id we sell. A transaction naming anything else is
    # rejected, so a purchase of some other product can never grant a plan
    # here. Price is deliberately NOT configured: StoreKit is the source of
    # truth and the app renders the localized price off the product itself.
    APPLE_IAP_PRODUCT_ID: str = ""

    # openclaw.json schema gate (core/services/config_schema.py). "audit" logs
    # + meters violations and lets the write through; "enforce" rejects it.
    # Default audit because a wrong vendored schema would otherwise brick config
    # delivery fleet-wide — untrusted marketplace slices are enforced regardless
    # of this knob, so the risky input never rides on the default.
    OPENCLAW_CONFIG_SCHEMA_MODE: str = "audit"

    # Marketplace (set by CDK ServiceStack — empty default lets the backend
    # boot in non-marketplace contexts like local unit tests).
    MARKETPLACE_LISTINGS_TABLE: str = ""
    MARKETPLACE_PURCHASES_TABLE: str = ""
    MARKETPLACE_PAYOUT_ACCOUNTS_TABLE: str = ""
    MARKETPLACE_TAKEDOWNS_TABLE: str = ""
    MARKETPLACE_SELLERS_TABLE: str = ""
    MARKETPLACE_ARTIFACTS_BUCKET: str = ""

    # Litestream replica bucket for the local-sqlite container layout (set
    # by CDK ServiceStack per env, dev-first). Empty means the env still runs
    # the EFS state layout: the backend can reach hot SQLite directly on EFS
    # and every local-sqlite code path (sentinel, S3 replica lifecycle) stays inert.
    LITESTREAM_BUCKET: str = ""

    # Provisioned agent email (set by CDK ServiceStack). This is the BASE
    # domain (e.g. "isol8.co" in prod) — each company gets its own subdomain,
    # so an address is "{agent}@{slug}.{AGENT_EMAIL_DOMAIN}" (e.g.
    # "friday@acme-robotics.isol8.co"). Empty disables address minting
    # entirely, so an env without a verified mail domain never mints.
    AGENT_EMAIL_DOMAIN: str = ""

    # SES configuration set every agent-mail send is stamped with (the
    # complaint event destination hangs off it). Empty ⇒ sends carry no
    # configuration set — fine for envs without the outbound stack.
    AGENT_MAIL_CONFIGURATION_SET: str = ""

    # Stripe Connect (seller payout onboarding) — environment-specific URLs
    # set by CDK so the marketplace UI can return the user back to the right
    # host after Stripe-hosted onboarding.
    STRIPE_CONNECT_REFRESH_URL: str = ""
    STRIPE_CONNECT_RETURN_URL: str = ""

    # Marketplace webhook signing secrets. TWO Stripe endpoint registrations
    # deliver to POST /webhooks/stripe-marketplace, each with its own secret:
    # a platform-scoped one for checkout/refund events (they originate on the
    # platform balance, so Stripe only delivers them to "events on your
    # account" endpoints) and a Connect-scoped one for connected-account
    # account.updated. Both are separate from STRIPE_WEBHOOK_SECRET (the
    # billing webhook). The route tries MARKETPLACE first, then CONNECT.
    STRIPE_MARKETPLACE_WEBHOOK_SECRET: str = ""
    STRIPE_CONNECT_WEBHOOK_SECRET: str = ""

    # Platform take rate on paid marketplace listings, in whole percent.
    # Applied at fulfillment: the seller's held-balance credit (and later
    # Transfer) is price * (100 - fee) / 100; the buyer always pays the
    # listed price. 15 = launch decision 2026-08-13.
    MARKETPLACE_PLATFORM_FEE_PERCENT: int = 15

    # Encryption (base64-encoded 32-byte key for Fernet encryption of BYOK API keys)
    ENCRYPTION_KEY: str = ""

    # Shared password for the gog CLI file-keyring backend. The gog_cli
    # materializer runs gog with GOG_KEYRING_BACKEND=file and this password to
    # encrypt the owner's Google OAuth keyring on EFS; the identical value is
    # injected into every per-user OpenClaw container so it can read the keyring
    # at runtime. Provisioned by CDK from the `isol8/{env}/gog-keyring-password`
    # secret — must stay stable forever or existing keyrings become unreadable.
    GOG_KEYRING_PASSWORD: str = ""

    # KMS CMK ARN/alias for per-container secrets (operator device private keys,
    # gateway tokens). Provisioned by CDK as `alias/isol8-{env}-container-secrets`.
    # Backend calls kms:Encrypt at provision time and kms:Decrypt at handshake
    # time — both operations are audited via CloudTrail so we have a per-call
    # record of which backend instance touched which container's secrets.
    CONTAINER_SECRETS_KMS_KEY_ID: str = ""

    # --- Paperclip integration ---
    # Internal URL the backend uses to reach Paperclip's REST API (private subnet).
    # e.g. ``http://paperclip.internal.isol8.local`` — set by CDK in deployed envs.
    PAPERCLIP_INTERNAL_URL: str = ""
    # Public-facing URL for browsers (used by the proxy router for cookie scope
    # and origin checks; T14/T15 introduce the actual proxy). e.g.
    # ``https://company.isol8.co``.
    PAPERCLIP_PUBLIC_URL: str = ""
    # HMAC key for the OpenClaw service-token JWTs the backend mints for
    # seeded Paperclip agents (see core/services/service_token.py). Populated
    # from Secrets Manager + KMS by the CDK service stack at deploy time.
    # Re-used by paperclip_proxy to sign the proxy's own session cookie.
    PAPERCLIP_SERVICE_TOKEN_KEY: str = ""
    # Shared bearer token the isol8.notifications Paperclip plugin sends on
    # POST /webhooks/paperclip-events. Empty disables the receiver (404).
    # Populated from Secrets Manager by the CDK service stack; the same secret
    # is injected into the Paperclip task as PAPERCLIP_ISOL8_NOTIFY_TOKEN by
    # paperclip-stack (the plugin worker reads it via an env passthrough —
    # plugin configJson is per-company on the deployed fork line, so env is
    # the only instance-wide channel).
    PAPERCLIP_EVENTS_WEBHOOK_SECRET: str = ""
    # The `hub.verify_token` Meta echoes back on the one-time subscribe
    # handshake for the APP-LEVEL webhook callback (routers/meta_webhook.py).
    # It is a shared string of our choosing, set identically here and in the
    # Meta app's webhook config; it authenticates nothing after setup (real
    # deliveries carry X-Hub-Signature-256 instead). Empty ⇒ the whole /hooks/meta
    # route 503s, so a half-configured environment can never complete a
    # handshake and start receiving events it cannot verify.
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    # Clerk publishable key (the same value the frontend uses as
    # NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY). The paperclip-proxy bootstrap page
    # renders inline JS that loads the Clerk SDK and handshakes a session
    # cookie; the SDK needs this key to identify which Clerk instance to
    # talk to. Publishable keys are public by design — Clerk literally
    # serves them in HTML; storing in env (not Secrets Manager) is correct.
    CLERK_PUBLISHABLE_KEY: str = ""
    # Public URL of the frontend (e.g. https://dev.flickmeon.ai). Used by
    # the paperclip-proxy to redirect users back to /chat when they land on
    # company.isol8.co without prerequisites (no container, no Clerk
    # session, no Paperclip company row). Same value as CORS_ORIGINS in
    # most envs but kept separate so the proxy doesn't need to parse a
    # comma-separated list.
    FRONTEND_URL: str = "https://dev.flickmeon.ai"

    # --- Transactional email ---
    # OUTBOUND app-generated mail (the welcome email on signup, support-ticket
    # delivery into the support@ inbox) goes out via AWS SES — the same
    # transport as agent replies, see core/services/ses_email.py. The inbound
    # support mailbox itself is Google Workspace, read by the founder's agent.
    #
    # There is no separate enable flag or API key: sending is gated on
    # AGENT_EMAIL_DOMAIN, which holds the verified SES domain and is set only
    # where the prod-only SesInboundStack created that identity.
    #
    # Shared secret the SES-ingest Lambda presents to POST /webhooks/ses-inbound.
    # Empty disables the SES inbound endpoint entirely (fail-closed).
    SES_INBOUND_SECRET: str = ""
    # Visible From identities on the verified platform mail domain. CDK DOES
    # wire all three (service-stack.ts, off lib/domains.ts PLATFORM_MAIL_DOMAIN),
    # so these defaults only apply to local dev — keep them in step with the
    # mail domain anyway, or a local run sends as a domain SES will refuse.
    EMAIL_FROM_WELCOME: str = "Lightbulb <hello@thelightbulb.company>"
    # The Google Workspace support mailbox. Used as Reply-To on the welcome
    # email (a plain reply lands in the inbox the agent watches) and (Plan 2)
    # as the ticket destination.
    EMAIL_SUPPORT_ADDRESS: str = "support@thelightbulb.company"
    # Sender identity for support-ticket delivery emails. Lands in the
    # EMAIL_SUPPORT_ADDRESS inbox; distinct from welcome's hello@ so a Gmail
    # filter / the agent can tell app-generated tickets from direct customer mail.
    EMAIL_FROM_TICKETS: str = "Lightbulb Support <tickets@thelightbulb.company>"

    @field_validator("CLERK_ISSUER")
    @classmethod
    def validate_clerk_issuer(cls, v: str) -> str:
        if "your-clerk-domain" in v:
            raise ValueError("CLERK_ISSUER not configured. Set the CLERK_ISSUER environment variable.")
        return v

    @field_validator("CLERK_AZP_MODE")
    @classmethod
    def validate_clerk_azp_mode(cls, v: str) -> str:
        if v not in ("audit", "enforce"):
            raise ValueError(f"CLERK_AZP_MODE must be 'audit' or 'enforce'; got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_deployed_security(self) -> "Settings":
        """Fail LOUD at boot rather than silently fail open in a deployment.

        Several security fences default to the *insecure* value (WS impersonation
        enforcement off) or hinge on ``ENVIRONMENT`` reading exactly ``"prod"``
        (the debug routes + Clerk webhook signature guard fail open otherwise). A
        dropped CDK variable would open these with no other signal, so assert the
        invariants at construction — the app refuses to boot if they break.
        """
        raw = self.ENVIRONMENT
        env = raw.strip().lower()

        # Downstream guards compare ``settings.ENVIRONMENT == "prod"`` EXACTLY,
        # so a non-canonical "PROD" / " prod " would pass the checks here yet
        # fail open there. Require the canonical form.
        if raw != env:
            raise ValueError(f"ENVIRONMENT must be lowercase with no surrounding whitespace; got {raw!r}")
        # Catch a typo'd ENVIRONMENT ("prd") that silently reads as non-prod.
        if env and env not in _KNOWN_ENVIRONMENTS:
            raise ValueError(f"ENVIRONMENT={raw!r} is not one of {sorted(_KNOWN_ENVIRONMENTS)}")

        # The fence assertions below apply ONLY to the actual backend task. The
        # Fargate metadata URI is always injected by the agent for the running
        # container and is NEVER present locally, in CI, or in out-of-task
        # operational CLIs (scripts.bedrock_gate_break_glass /
        # scripts.reapply_openclaw_config set ENVIRONMENT=prod to target prod
        # resources but don't serve /ws and needn't carry the WS secret).
        if not os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
            return self

        # We ARE the deployed backend. ENVIRONMENT must say so (a non-deployed
        # value here is a dropped CDK var that flips debug/webhook guards open).
        if env not in _DEPLOYED_ENVIRONMENTS:
            raise ValueError(
                "Running in ECS Fargate but ENVIRONMENT is not a deployed value "
                f"(got {raw!r}); refusing to boot to avoid fail-open debug/webhook guards."
            )
        # WS enforcement IS wired by the CDK (service-stack.ts sets
        # WS_ENFORCE_INTERNAL_SECRET="true" + WS_INTERNAL_SECRET), so a missing
        # value here is a REGRESSION — fail closed.
        if not (self.WS_ENFORCE_INTERNAL_SECRET and self.WS_INTERNAL_SECRET):
            raise ValueError(
                "WS internal-caller enforcement must be ON with a secret in a deployed environment; "
                "otherwise /ws/connect can be re-bound to another user via forged headers."
            )
        # SEC-8: token-origin (`azp`) verification. In enforce mode an empty
        # allowlist would silently verify nothing while looking secure — fail
        # closed. In audit mode, warn loudly so the gap can't go quiet again.
        if self.CLERK_AZP_MODE == "enforce" and not self.clerk_authorized_parties_list:
            raise ValueError(
                "CLERK_AZP_MODE=enforce requires CLERK_AUTHORIZED_PARTIES to be set in a deployed environment (SEC-8)."
            )
        if self.CLERK_AZP_MODE != "enforce":
            logging.getLogger(__name__).warning(
                "Clerk azp verification is in AUDIT mode — token-origin mismatches are logged, "
                "not rejected. Flip CLERK_AZP_MODE=enforce once audit shows zero mismatches (SEC-8)."
            )
        return self

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
