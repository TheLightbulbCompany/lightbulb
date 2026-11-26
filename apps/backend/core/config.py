import logging
import os

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# ENVIRONMENT values that mean "this is a real, internet-reachable deployment"
# (vs. "local" / "test" / unset for local dev + CI). Deployed envs must have the
# security fences configured — see _validate_deployed_security below.
_DEPLOYED_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
_KNOWN_ENVIRONMENTS = _DEPLOYED_ENVIRONMENTS | frozenset({"local", "test"})


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
    # Clerk user ids granted platform admin (/admin, /ops, marketplace review,
    # workspace wipe). Comma-separated, set per env by CDK (service-stack.ts).
    # Empty ⇒ nobody is admin. Never derived from an email claim.
    PLATFORM_ADMIN_USER_IDS: str = ""
    # Extra Clerk user ids (team members, test accounts) excluded from the
    # admin Spark metrics. Comma-separated; PLATFORM_ADMIN_USER_IDS are excluded too.
    SPARK_INTERNAL_USER_IDS: str = ""

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

    # Public-facing base URL for this API (Spark's billing links). Per env:
    # https://api-dev.thelightbulb.company, https://api.thelightbulb.company.
    PUBLIC_API_BASE: str = "http://localhost:8000"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS_ORIGINS as comma-separated list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def clerk_authorized_parties_list(self) -> list[str]:
        """Parse CLERK_AUTHORIZED_PARTIES as comma-separated list."""
        return [p.strip() for p in self.CLERK_AUTHORIZED_PARTIES.split(",") if p.strip()]

    @property
    def platform_admin_user_ids(self) -> frozenset[str]:
        """Parse PLATFORM_ADMIN_USER_IDS as a comma-separated set."""
        return frozenset(u.strip() for u in self.PLATFORM_ADMIN_USER_IDS.split(",") if u.strip())

    @property
    def spark_internal_user_ids(self) -> frozenset[str]:
        """SPARK_INTERNAL_USER_IDS plus platform admins: the accounts Spark metrics ignore."""
        extra = {u.strip() for u in self.SPARK_INTERNAL_USER_IDS.split(",") if u.strip()}
        return frozenset(extra) | self.platform_admin_user_ids

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

    OPENAI_API_KEY: str = ""
    """Platform key for GPT-Live voice sessions. From Secrets Manager. Not a real ``sk-`` key ⇒ calls 503 voice_unavailable."""

    # Billing / Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    #: Spark's $20/month subscription (one licensed price); empty: no checkout link.
    STRIPE_SPARK_PRICE_ID: str = ""
    #: Save Spark voice transcripts (text only, 90 days) to the spark-transcripts table. On in dev and prod
    #: (set by CDK); off: nothing is saved.
    SPARK_TRANSCRIPTS_ENABLED: bool = False
    #: Spark data store (plain objects in S3). Off: nothing is written there.
    SPARK_STORE_ENABLED: bool = False
    #: Work-log upload scope: "problem" keeps text only for failed or cancelled tasks, "all" keeps every task.
    SPARK_WORKLOG_SCOPE: str = "problem"
    # Display dollars for STRIPE_SPARK_PRICE_ID (Spark's monthly price); lockstep with its unit amount.
    SPARK_PRICE_USD: int = 20
    # What a minute of Spark voice costs us (gpt-live-1.1): a calibration value for the margin metric, unverified.
    SPARK_VOICE_USD_PER_MIN: float = 0.05

    # HMAC key for service-token JWTs (``service_token.py``): Spark's checkout and portal links.
    PAPERCLIP_SERVICE_TOKEN_KEY: str = ""
    # Public URL of the frontend (e.g. https://dev.thelightbulb.company).
    FRONTEND_URL: str = "https://dev.thelightbulb.company"

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

        Several security fences hinge on ``ENVIRONMENT`` reading exactly ``"prod"``
        (the Clerk webhook signature guard fails open otherwise). A
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
        # container and is NEVER present locally, in CI, or in out-of-task CLIs.
        if not os.environ.get("ECS_CONTAINER_METADATA_URI_V4"):
            return self

        # We ARE the deployed backend. ENVIRONMENT must say so (a non-deployed
        # value here is a dropped CDK var that flips the webhook guard open).
        if env not in _DEPLOYED_ENVIRONMENTS:
            raise ValueError(
                "Running in ECS Fargate but ENVIRONMENT is not a deployed value "
                f"(got {raw!r}); refusing to boot to avoid a fail-open webhook guard."
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
