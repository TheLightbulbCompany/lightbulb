# Thirty-day trial and included Muse rollout

Implementation is disabled by default for Muse. No live provider or Stripe configuration is created by the code change. Existing trial deadlines and paid subscriptions are preserved.

## Configuration

- Backend and the existing inference gate use the same CDK context switches `museEnabledDev` and `museEnabledProd`. Both default false. Enable dev first, after the provider agreement and smoke fixture are ready.
- The gate alone reads `lightbulb/gate/{env}/opencode-zen` from Secrets Manager. Store the raw Zen API key as the secret's `SecretString` (not JSON); do not put the vendor key in a tenant profile or backend environment. The prefix is outside the backend's ordinary owner-secret grant.
- Supply `STRIPE_ENTRY_PRICE_ID_DEV` and `STRIPE_ENTRY_PRICE_ID_PROD` through the existing GitHub/CDK environment-specific configuration. Each must identify its environment's $20 monthly workspace Price. Preserve the existing $50/Ultra Price IDs. Entry checkout remains unavailable until its price and Muse are configured.
- `museMaxOutputTokens` (default 16,384) is a configurable request ceiling, not a monthly allowance or a guarantee of unlimited inference. This is a conservative service ceiling, not a verified native model maximum; review it against real reasoning/tool workloads before launch to avoid truncating reasoning.

## Required checks before enabling customer traffic

1. Confirm the Zen agreement covers sponsored multi-tenant autonomous use and the chosen standard model. Contributor-free training participation is not selected by this change.
2. Use a disposable dev workspace to verify no-card signup, server-ready 30-day clock, first response, listening, nightly automation, images, helper/compaction/delegated calls, and a Responses stream lasting over 30 seconds.
3. Verify expired/unarmed trials, revoked owner tokens, wrong models, malformed requests, upstream rate limits and canceled/disconnected streams. The vendor key must be absent from runtime files/environment and logs. Included Muse must work with zero Bedrock credits.
4. Exercise Entry checkout, Entry→Workspace with Muse retained, Workspace→Entry with configuration applied, and existing BYOK/ChatGPT/Claude credit paths. Replay delayed subscription events and interrupt a provider update to verify durable recovery.
5. Inspect legacy denied inference-token rows before enabling existing owners. Old generic denials do not prove a monetary reason. Do not bulk-clear them. Verify both existing Bedrock restoration and new generic revocation behavior on dev.

## Deployment order

Use CI/CD: deploy gate support and backend configuration, build the updated Hermes image, promote its dev pin, and roll a dedicated dev runtime. A pin flip or S3 profile write alone does not refresh a warm runtime. Verify the actual runtime image and provider after the roll. Enable the dev capability only with a working key and aligned gateway/backend switches. Complete the checks above before production promotion and any existing-owner rollout.

An existing owner's stored deadline does not become 30 days automatically. Any extension requires a separately selected cohort and conditional migration. Do not reset paid accounts or erase profiles. Expiry offers an explicit upgrade; it never creates a charge automatically.

Rollback must consider active Muse/Entry subscriptions: turning Muse off blocks their inference. Revert to a verified working image/route where possible; do not silently switch them to paid Claude or charge their credit balance.
