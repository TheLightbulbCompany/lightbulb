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
    # The A2A door also accepts a grant for the /mcp audience that carries an A2A scope: only if Clerk
    # does not echo the requested /a2a resource into `aud` (checked once against the dev instance).
    A2A_ACCEPT_MCP_AUDIENCE: bool = False
    # Hosted A2A sends to laptop sessions ride the workspace's cloud member first: it signs in as the
    # workspace (CLI v0.9.11+). A laptop that refuses what it relays falls back to the person's link.
    A2A_DISPATCH_VIA_MEMBER: bool = True
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
    # Clerk user ids granted platform admin (/admin, /ops, marketplace review,
    # workspace wipe). Comma-separated, set per env by CDK (service-stack.ts).
    # Empty ⇒ nobody is admin. Never derived from an email claim.
    PLATFORM_ADMIN_USER_IDS: str = ""

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
    # responses (e.g., the trigger fire URLs, /hooks/t/...).
    # Per env: https://api-dev.thelightbulb.company, https://api.thelightbulb.company,
    # https://api.isol8.co. Defaults to localhost for dev/test. Wired via
    # CDK service-stack environment block per environment.
    PUBLIC_API_BASE: str = "http://localhost:8000"

    # OpenAI's plugin portal token, served at /.well-known/openai-apps-challenge to prove we own the
    # MCP host. Public by design. Empty: the path 404s. CDK sets it from OPENAI_APPS_CHALLENGE_TOKEN_{ENV}.
    OPENAI_APPS_CHALLENGE_TOKEN: str = ""

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

    def meetings_available(self) -> bool:
        """Meetings need a LiveKit project and a Recall key; dev has no LiveKit, so it books nothing."""
        return bool(self.LIVEKIT_URL and self.RECALL_API_KEY)

    @property
    def clerk_authorized_parties_list(self) -> list[str]:
        """Parse CLERK_AUTHORIZED_PARTIES as comma-separated list."""
        return [p.strip() for p in self.CLERK_AUTHORIZED_PARTIES.split(",") if p.strip()]

    @property
    def platform_admin_user_ids(self) -> frozenset[str]:
        """Parse PLATFORM_ADMIN_USER_IDS as a comma-separated set."""
        return frozenset(u.strip() for u in self.PLATFORM_ADMIN_USER_IDS.split(",") if u.strip())

    # --- Workspace runtime networking ---
    # The VPC placement an owner's AgentCore runtime is created with
    # (core/services/agentcore_runtime.py). Named for the ECS-era CDK outputs that still
    # feed them; there is no ECS cluster or task definition any more.
    ECS_SUBNETS: str = ""  # comma-separated
    ECS_SECURITY_GROUP_ID: str = ""

    # PROVISION Step Functions state machine ARN — THE provisioning executor
    # (spec §7, PR-10; no flag, active on merge). Dispatched by
    # core.services.provision_dispatch.dispatch_provision via StartExecution;
    # the one-shot scripts/provision_runner.py runs in the machine's provisioner
    # Lambda, never in-process in this backend.
    PROVISION_STATE_MACHINE_ARN: str = ""

    # Public S3 URL of the hosted AWS read-only connector CloudFormation
    # template (the public-read lightbulb-{env}-cfn-templates bucket, wired by CDK
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

    # Webhook event-id dedup table (Stripe + Clerk). Conditional PutItem with
    # attribute_not_exists drops replays at the edge. Wired via CDK service-stack.
    WEBHOOK_DEDUP_TABLE: str = ""

    # WebSocket Configuration (API Gateway Management API)
    WS_CONNECTIONS_TABLE: str = "lightbulb-dev-ws-connections"
    WS_MANAGEMENT_API_URL: str = ""  # Set by Terraform
    # Public WSS endpoint returned by POST /api/v1/desktop/pair so the desktop
    # local-session relay knows which API Gateway door to connect to (it needs
    # the Management API, so it stays there). Wired by CDK service-stack from
    # api-stack's WebSocket stage URL.
    WS_GATEWAY_URL: str = "wss://ws-dev.thelightbulb.company"
    # Raw WebSocket relay for machine clients (desktop node hosts): one backend task pipes both sockets for the connection's
    # life instead of the API Gateway relay above. Same `bv-` CloudFront/ALB
    # door as the browser-view live view, path-capped to /api/v1/gateway/*.
    # Wired by CDK service-stack from api-stack's gateway-relay-url output.
    GATEWAY_RELAY_URL: str = "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws"

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
    # Seeds the Ed25519 key that signs lightbulb login proofs (network_proof).
    NETWORK_PROOF_KEY: str = ""
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
    METRONOME_VOICE_RATE_CARD_ID: str = ""
    METRONOME_VOICE_METRIC_ID: str = ""
    METRONOME_VOICE_SUBSCRIPTION_PRODUCT_ID: str = ""
    METRONOME_VOICE_ENROLLMENT_ENABLED: bool = False
    OPENAI_API_KEY: str = ""
    """Platform key for GPT-Live voice sessions. From Secrets Manager. Not a real ``sk-`` key ⇒ calls 503 voice_unavailable."""

    # Huddles (LiveKit). Empty LIVEKIT_URL = huddle routes answer 503 huddles_unavailable.
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: str = ""
    LIVEKIT_API_SECRET: str = ""
    LIVEKIT_AGENT_NAME: str = "lightbulb-huddle"
    # The one phone line (E.164) this environment gives out; its LiveKit dispatch rule routes to this env's agent.
    VOICE_LINE_NUMBER: str = ""
    # Buying a number per business at Go live (voice_onboarding.provision_at_go_live). Off until Twilio KYC clears;
    # while off, Go live gives VOICE_LINE_NUMBER. The trunk is the Twilio Elastic SIP trunk that points at LiveKit.
    VOICE_BUY_NUMBERS: bool = False
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_API_KEY_SID: str = ""
    TWILIO_API_KEY_SECRET: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_TRUNK_SID: str = ""
    VOICE_INBOUND_TRUNK_ID: str = ""
    # The LiveKit outbound SIP trunk (ST_...) the forwarding check dials on. Empty = the check answers outbound_not_configured.
    VOICE_OUTBOUND_TRUNK_ID: str = ""
    """The worker huddles and meetings dispatch to. Prod's deployed agent keeps the default; dev's Mac worker is lightbulb-meet-dev."""
    HUDDLE_WORKER_SECRET: str = ""
    # Meetings (Recall.ai bots). See meetings_available(): without LiveKit + a Recall key an invite opens the room only.
    RECALL_API_BASE: str = "https://us-east-1.recall.ai"
    RECALL_API_KEY: str = ""
    RECALL_WEBHOOK_SECRET: str = ""
    """The ``whsec_`` workspace verification secret. Empty = every Recall webhook is refused."""
    RECALL_GOOGLE_LOGIN_GROUP_ID: str = ""
    """Recall Google Login Group the Meet bot signs in with. Empty = joins signed out (and may wait in the lobby)."""
    # Text agents (Photon iMessage/SMS). Empty = the texting rail is off.
    PHOTON_API_KEY: str = ""  # Photon 2.0 project key, pho_sk_...
    PHOTON_PROJECT_ID: str = ""  # pho_prj_...
    PHOTON_WEBHOOK_SECRET: str = ""  # the Standard-Webhooks whsec_... secret
    # removed with the Sendblue client in plan 4 Task 2
    SENDBLUE_API_KEY_ID: str = ""
    SENDBLUE_API_SECRET_KEY: str = ""
    SENDBLUE_WEBHOOK_SECRET: str = ""
    MEET_BOT_PAGE_URL: str = ""
    """The bot's Output Media page. Empty = ``{FRONTEND_URL}/meet-bot``."""
    OPENROUTER_MARKUP: str = ""
    """Managed-credit markup (repo-root openrouter-pricing.json, injected by CDK). Voice debits use it."""
    METRONOME_WEBHOOK_SECRET: str = ""
    """HMAC-SHA256 key for verifying Metronome-Webhook-Signature on inbound webhooks."""
    METRONOME_FAST_GATE_TABLE: str = ""
    """DDB table backing the local fast-gate flag the synchronous chat gate reads."""
    OPENROUTER_WORKSPACE_ID: str = ""
    OPENROUTER_TRIAL_KEY_DAILY_LIMIT_USD: float = 0.25
    """OpenRouter per-key daily $ limit for a trial_local owner (the hard valve)."""
    OPENROUTER_BASE_KEY_DAILY_LIMIT_USD: float = 0.25
    """OpenRouter per-key daily $ limit for every other managed owner."""
    OPENROUTER_PAID_KEY_DAILY_LIMIT_USD: float = 50.0
    """Backstop key limit for explicit managed paid owners, whose real valve is Metronome."""
    METRONOME_RATE_CARD_ID: str = ""
    """Rate card the per-customer contract references at provision time.
    Created once per env in the Metronome console (runbook step)."""
    METRONOME_CREDITS_PRODUCT_ID: str = ""
    """Metronome product id the prepaid credit commit draws against. Per-env."""
    METRONOME_STRIPE_DELIVERY_METHOD_ID: str = ""
    """The Stripe integration Metronome bills through. Required by Metronome once an env has more
    than one Stripe integration (dev keeps a legacy one until Metronome support removes it);
    empty sends the bare delivery_method, which only resolves with exactly one."""
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

    SUPER_AGENT_FAULTS: str = ""
    """Dev-only fault hooks, ``name[:param],...`` (``core.services.super_agent_faults``). Refused in prod."""
    # Temporal (contracts-v2 §16). Defaults are the local dev server; dev sets the Cloud namespace and
    # injects the key from Secrets Manager ``lightbulb/dev/temporal_api_key``. Key set = API key + TLS.
    TEMPORAL_ADDRESS: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_API_KEY: str | None = None
    TEMPORAL_TASK_QUEUE: str = "lightbulb"
    # Watchdog (spec 11): its own queue and ECS service; `#watchdog` = Lightbulb's own dev workspace + room.
    TEMPORAL_WATCHDOG_TASK_QUEUE: str = "lightbulb-watchdog"
    WATCHDOG_OWNER_ID: str = ""
    WATCHDOG_ROOM_ID: str = ""

    TRIAL_WAKE_GATE_ENABLED: bool = True
    """Kill switch for reporting an idle trial workspace as parked. Off = every
    wake_policy entry point answers "not parked", so no surface renders "Agents
    asleep". It is a REPORT, not a gate: AgentCore wakes the microVM on the next
    invoke, so nothing is being held back either way."""

    # The per-owner IAM role (each workspace's AgentCore execution role). Empty defaults keep the
    # backend booting in non-AWS unit-test contexts.
    BEDROCK_OWNER_BOUNDARY_ARN: str = ""
    """Permissions-boundary policy ARN every backend-minted role must carry."""
    BEDROCK_OWNER_BASE_POLICY_ARN: str = ""
    """CDK-managed EFS/logs/ssm managed-policy ARN attached to every per-owner role."""
    OPENROUTER_GATE_URL: str = ""
    OPENROUTER_MANAGED_MODEL_ID: str = ""

    # Billing / Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_WORKSPACE_PRICE_ID: str = ""
    STRIPE_BASE_PRICE_ID: str = ""
    # Stripe price for the Ultra runtime tier. Empty default so the app boots
    # without it; CDK-wired in dev/prod. This is the billing source of truth
    # for "$150".
    STRIPE_ULTRA_PRICE_ID: str = ""
    # $20/month per human member from the 6th (billing_service.SEATS_INCLUDED).
    # Empty = fail closed: a 6+ member checkout is refused and no seat is billed.
    STRIPE_SEAT_PRICE_ID: str = ""
    # $100/month Front desk add-on (a third subscription item; pays for the workspace's texting number).
    # Empty = fail closed: the add-on cannot be bought.
    STRIPE_FRONT_DESK_PRICE_ID: str = ""
    # Display dollars for the Workspace plan, echoed to clients via
    # /billing/account. Kept in lockstep with STRIPE_WORKSPACE_PRICE_ID's unit
    # amount in Stripe. The iOS paywall renders the web price from this, and
    # App Review requires a displayed external price to be accurate, so a
    # divergence here is a compliance bug rather than a cosmetic one.
    WORKSPACE_PRICE_USD: int = 50
    BASE_PRICE_USD: int = 20
    ULTRA_PRICE_USD: int = 150
    # Display dollars for STRIPE_SEAT_PRICE_ID (per member from the 6th); lockstep with its unit amount.
    SEAT_PRICE_USD: int = 20
    # Display dollars for STRIPE_FRONT_DESK_PRICE_ID (Photon's $250/month line price); lockstep with its unit amount.
    FRONT_DESK_PRICE_USD: int = 250

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
    # Selling switch. When False (prod until launch) /billing/account lists no
    # products, purchase-intent 404s, and the app renders no paywall. The
    # notification webhook works either way (historical subscribers).
    APPLE_IAP_SELLING: bool = False
    # DEV ONLY: accept Xcode StoreKit Testing transactions, which are signed by a
    # local Xcode key, not Apple. apple_iap refuses them whenever ENVIRONMENT is
    # "prod" regardless of this flag, and CDK never sets it for prod.
    APPLE_IAP_ACCEPT_XCODE: bool = False

    # EVERY per-owner file the platform stores: the Hermes profiles
    # (config.yaml/.env/SOUL.md — see core.services.hermes_profiles), the SQLite replicas the
    # runtime restores at session start, marketplace install payloads, and room attachments.
    # One bucket, one per-owner ``<owner>/*`` IAM grant. Set by CDK ServiceStack per env; empty
    # means no owner state can be written, which the writers refuse rather than guess around.
    OWNER_STATE_BUCKET: str = ""
    # The Hermes bridge image a workspace's AgentCore runtime runs. Empty = provision_hermes_runtime
    # refuses rather than guessing an image.
    AGENTCORE_HERMES_IMAGE: str = ""
    # The env's shared AgentCore Memory (CDK ContainerStack.workspaceMemory). Empty = no workspace
    # memory: no per-owner grant, no AGENTCORE_MEMORY_ID in the runtime env, no wipe.
    AGENTCORE_MEMORY_ID: str = ""
    # The role the backend assumes to hand each runtime session credentials scoped to its owner
    # (runtime_transport._session_credentials_header). Its grant must cover every owner's
    # ``<owner>/*`` in OWNER_STATE_BUCKET and the workspace memory; the session policy narrows it
    # to one owner. Empty = no scoped credentials, the bridge uses its execution role (today).
    HERMES_TENANT_ROLE_ARN: str = ""
    # The tier's ONE pooled Hermes runtime (CDK ServiceStack HermesSharedRuntime). Set: every
    # workspace's sessions run there (runtime_transport.runtime_arn), each its own microVM, and a
    # new workspace gets no runtime, role or boot secret of its own. Names infra, not a flag.
    HERMES_SHARED_RUNTIME_ARN: str = ""
    # The env's uploaded-sessions AgentCore Memory (CDK ContainerStack.sessionsMemory): the evaluator's
    # episodes and reflections, apart from the workspace memory. Empty = no evaluator.
    AGENTCORE_SESSIONS_MEMORY_ID: str = ""
    # Haiku-class model that splits a recall question into the facts it needs (memory_models.expand);
    # the same id container-stack.ts gives AgentCore extraction (MEMORY_EXTRACT_MODEL).
    MEMORY_EXPAND_MODEL: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    # The env's VPC-mode workspace browser (CDK: ContainerStack.workspaceBrowser), which leaves
    # from the NAT's fixed address. Empty (local, tests) = AWS's built-in browser.
    AGENTCORE_BROWSER_ID: str = ""

    # Provisioned agent email (set by CDK ServiceStack). This is the BASE
    # domain (e.g. "thelightbulb.company" in prod) — each company gets its own
    # subdomain and ONE team address, "agents@{slug}.{AGENT_EMAIL_DOMAIN}" (e.g.
    # "agents@acme-robotics.thelightbulb.company"). Empty disables address minting
    # entirely, so an env without a verified mail domain never mints.
    AGENT_EMAIL_DOMAIN: str = ""

    # The Lightbulb workspace's Clerk org id. Every OTHER org workspace gets a
    # platform-managed channel shared with this org (core/services/messaging.py
    # ensure_vendor_room). Empty disables the feature entirely.
    VENDOR_ORG_ID: str = ""

    # SES configuration set every agent-mail send is stamped with (the
    # complaint event destination hangs off it). Empty ⇒ sends carry no
    # configuration set — fine for envs without the outbound stack.
    AGENT_MAIL_CONFIGURATION_SET: str = ""

    # Company Stripe account (the owner's own business account on our Connect
    # platform). Hosted onboarding bounces back to these; the Standard-OAuth
    # "connect existing" flow returns to the backend callback, which must be
    # registered verbatim as a redirect URI in Stripe → Connect settings.
    STRIPE_COMPANY_REFRESH_URL: str = ""
    STRIPE_COMPANY_RETURN_URL: str = ""
    STRIPE_COMPANY_OAUTH_REDIRECT_URL: str = ""
    # Connect platform client id (ca_…). Public, not a secret.
    STRIPE_CONNECT_CLIENT_ID: str = ""
    # The platform's publishable key (pk_…). Public, not a secret: Connect
    # embedded onboarding initialises with it. Empty ⇒ the Stripe card falls
    # back to the hosted Account Link.
    STRIPE_PUBLISHABLE_KEY: str = ""

    # The Accounts v2 API version for the account/account-link calls. The guide
    # pins them with `Stripe-Version: {{ACCOUNTS_V2_VERSION}}` and says to
    # confirm it during preview onboarding. Empty ⇒ the provisioning preview (2026-09-30.preview).
    STRIPE_ACCOUNTS_V2_VERSION: str = ""

    # Hosted assistants are offered network workers' assign tools (off: hidden, as in the engine).
    HOSTED_NETWORK_WORKERS: bool = False

    # Stripe Link Agent Wallet (agent purchases). Unconfigured (any of the three empty):
    # every route 404s and the UI hides. CLIENT_ID/SECRET come from Link's OAuth client
    # application; PUBLISHABLE_KEY is the Stripe pk that Link's /auth and /auth/token require.
    LINK_CLIENT_ID: str = ""
    LINK_CLIENT_SECRET: str = ""
    LINK_PUBLISHABLE_KEY: str = ""

    # --- Native multiplayer messaging (set by CDK ServiceStack; spec
    # docs/superpowers/specs/2026-09-09-native-multiplayer-messaging-architecture.md).
    # The env names are the frozen cross-packet contract — do not rename.
    MESSAGING_TABLE: str = ""
    """Canonical room/message table (``lightbulb-{env}-messaging``). Empty is
    only legal outside a deployed task; ``_validate_deployed_security`` fails
    boot without it because every room command would 500 at first use."""
    SEARCH_ENDPOINT: str = ""  # OpenSearch Serverless collection endpoint; empty disables message search
    OPENCLAW_WORK_QUEUE_URL: str = ""
    """FIFO wake queue (``lightbulb-{env}-openclaw-work.fifo``). Drained by the
    ``scripts/work_lambda.py`` Lambda (SQS event source), which deletes/re-times
    messages against this URL; the DLQ tools derive their queue from it."""
    APPSYNC_EVENTS_HTTP_ENDPOINT: str = ""
    """AppSync Events HTTP publish endpoint (ends ``/event``). Empty makes the
    live publisher a metric-only no-op."""
    APPSYNC_EVENTS_REALTIME_ENDPOINT: str = ""
    """AppSync Events realtime endpoint (ends ``/event/realtime``) handed to
    browsers by ``GET /messaging/live-config``."""
    APPSYNC_EVENTS_REGION: str = ""
    """Region the Event API lives in — the SigV4 signing region, which is not
    necessarily ``AWS_REGION``."""

    # Encryption (base64-encoded 32-byte key for Fernet encryption of BYOK API keys)
    ENCRYPTION_KEY: str = ""

    # Phase 2 task 5: integration credentials (website logins, API keys, OAuth blobs) go to the
    # credential-envelopes table under a KMS data key instead of one Secrets Manager secret each
    # ($0.40/mo). This is the CMK that wraps each data key (the stage's KMS key; infra sets it in
    # dev and prod). Empty (local) keeps writes on Secrets Manager.
    CREDENTIAL_KMS_KEY_ARN: str = ""

    # HMAC key for bedrock_gate_tokens.derive_gate_bearer (secret
    # lightbulb/{env}/gate_bearer_hmac_key). Dedicated, never ENCRYPTION_KEY; empty makes the
    # derivation refuse rather than fall back.
    GATE_BEARER_HMAC_KEY: str = ""

    # Shared password for the gog CLI file-keyring backend. The gog_cli
    # materializer runs gog with GOG_KEYRING_BACKEND=file and this password to
    # encrypt the owner's Google OAuth keyring on EFS; the identical value is
    # injected into every per-user OpenClaw container so it can read the keyring
    # at runtime. Provisioned by CDK from the `lightbulb/{env}/gog-keyring-password`
    # secret — must stay stable forever or existing keyrings become unreadable.
    GOG_KEYRING_PASSWORD: str = ""

    # Legacy env names (data names; the values are still configured by infra).
    # HMAC key for service-token JWTs (``service_token.py``), the digest/Needs-you email links and
    # the session-share signature.
    PAPERCLIP_SERVICE_TOKEN_KEY: str = ""
    # Shared bearer secret for ``routers/lifecycle_operations.py``.
    PAPERCLIP_EVENTS_WEBHOOK_SECRET: str = ""
    # The `hub.verify_token` Meta echoes back on the one-time subscribe
    # handshake for the APP-LEVEL webhook callback (routers/meta_webhook.py).
    # It is a shared string of our choosing, set identically here and in the
    # Meta app's webhook config; it authenticates nothing after setup (real
    # deliveries carry X-Hub-Signature-256 instead). Empty ⇒ the whole /hooks/meta
    # route 503s, so a half-configured environment can never complete a
    # handshake and start receiving events it cannot verify.
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    # Public URL of the frontend (e.g. https://dev.thelightbulb.company).
    FRONTEND_URL: str = "https://dev.thelightbulb.company"

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
        if env == "prod" and self.SUPER_AGENT_FAULTS.strip():
            raise ValueError("SUPER_AGENT_FAULTS is a dev-only fault hook list; it must be empty in prod.")

        # The fence assertions below apply ONLY to the actual backend task. The
        # Fargate metadata URI is always injected by the agent for the running
        # container and is NEVER present locally, in CI, or in out-of-task
        # operational CLIs (scripts.reap_owner_aips / scripts.roll_hermes_runtimes
        # set ENVIRONMENT=prod to target prod
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
        # The canonical messaging table is wired by the same CDK stack; a
        # missing value is a dropped variable, not a feature toggle.
        if not self.MESSAGING_TABLE:
            raise ValueError("MESSAGING_TABLE must be set in a deployed environment (native multiplayer rooms).")
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
