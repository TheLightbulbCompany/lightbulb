# Provisioned Agent Email — SES Transport (supersedes the Resend inbound design)

- **Date:** 2026-07-21
- **Status:** Design for review (pre-implementation)
- **Supersedes:** the mail-transport portions of `2026-07-19-provisioned-agent-email-design.md` (§6.4–§6.6, §9). Company identity, the address book, delivery into the agent, the reply-token, the relay, the `isol8-mail` tool, and the gates all carry over unchanged.

> **REVISION (2026-07-21, founder call) — read this first; it overrides the body where they differ:**
> - **Address is bare `friday@{slug}.isol8.co`** — no `agents.` layer. `AGENT_EMAIL_DOMAIN` = `isol8.co`.
> - **Prod-first.** Build for prod only; drop the dev/prod routing Lambda logic (one env, one backend). Dev inbound can come later.
> - **Regular `@isol8.co` mail is isolated.** The apex `isol8.co` MX stays on Resend (welcome/support); we add only a **wildcard MX `*.isol8.co → SES`** (matches subdomains, never the apex) + a **wildcard SPF `*.isol8.co`** (SES) + SES `isol8.co` DKIM. Agent mail (subdomains) and regular mail (apex) never collide. DMARC aligns under relaxed org-domain matching (`{slug}.isol8.co` ↔ `isol8.co`).
> - **Namespace guard:** company slugs become subdomains, so `RESERVED_SLUGS` MUST cover our infra subdomains (`api`, `ws`, `admin`, `app`, `www`, `mail`, `dev`, `staging`, …) so a company can't claim `api.isol8.co`. Audit + extend the list.
> - **Activation DNS is the delicate part** (wildcard MX/SPF + SES DKIM, verified not to disturb apex Resend mail) — a runbook step, not code.

## 1. Why this change

The desired address is **`friday@{slug}.agents.isol8.co`** — each company its own subdomain, each agent a mailbox on it. That requires *receiving* mail on arbitrary company subdomains. Research (well-sourced) found:

- **Resend cannot** wildcard/catch-all receive across subdomains — each subdomain must be registered + verified individually (a per-signup vendor call, undocumented domain cap). Unworkable at scale.
- **AWS SES can** — verify the parent domain once, one wildcard MX, one catch-all receipt rule → every company subdomain works with zero per-signup email-infra. It also hands us real SPF/DKIM/DMARC verdicts, and (since we get the raw message) makes attachment storage natural.

So the mail **transport** moves to SES for the agent-email feature. Resend stays for the app's own welcome/support mail (`isol8.co`), untouched.

## 2. Address scheme

- **Primary agent (Friday):** `friday@{slug}.agents.{base}` — e.g. `friday@acme-robotics.agents.isol8.co`.
- **Additional agents (later):** `atlas@{slug}.agents.{base}`, `sales@{slug}.agents.{base}` — the agent name is simply the local part. **No concatenation/truncation** → the review's per-agent-collision bug (B6) is deleted outright.
- `{base}` is per-env: **prod `isol8.co`**, **dev `dev.isol8.co`** — so prod mail domain is `agents.isol8.co`, dev is `agents.dev.isol8.co` (one shared Route53 zone; per-env subdomains avoid an MX collision).
- The slug is already lowercase `[a-z0-9-]`, ≤32 chars → a valid DNS label (max 63). No slug-format change needed.
- `AGENT_EMAIL_DOMAIN` is repurposed from the full mail domain to the **agent-mail base** (`agents.isol8.co` / `agents.dev.isol8.co`); the address is `{agent}@{slug}.{AGENT_EMAIL_DOMAIN}`.

## 3. Inbound architecture (SES)

```
sender → friday@acme-robotics.agents.isol8.co
  → wildcard MX (*.agents.isol8.co / *.agents.dev.isol8.co) → SES inbound (us-east-1)
  → ONE catch-all receipt rule (account-scoped rule set):
       action 1: S3 PutObject (raw MIME) into isol8-agent-mail-inbound bucket
       action 2: invoke the SES-ingest Lambda
  → Lambda (Python 3.12, non-VPC, stdlib `email` MIME parse):
       • read raw MIME from S3
       • parse from / to / subject / text / html / message-id / headers / attachments
       • read SES verdicts (spf/dkim/dmarc/spam/virus) from the SES event
       • route by recipient domain: *.agents.dev.isol8.co → dev backend, else prod
       • POST parsed payload → https://api[-dev].isol8.co/api/v1/webhooks/ses-inbound
         with header  X-Isol8-SES-Secret: <ses_ingest_secret>
  → backend POST /webhooks/ses-inbound (shared-secret, timing-safe compare — no Clerk):
       dedup(SES messageId) → resolve recipient (agent_email_repo.get_by_address)
       → sender allowlist → cap gate (trial_block_reason)
       → auth verdict (SES spf/dkim/dmarc — explicit FAIL dropped)
       → loop guard (Auto-Submitted/Precedence, now reliably present in raw MIME)
       → build reply-token + delivered message (+ attachment manifest)
       → deliver_to_agent (POST container /hooks/agent)  [UNCHANGED]
```

**Reuse map** (from grounding): the entire downstream — recipient resolution, allowlist, cap, loop guard, auth-verdict *logic*, reply-token, `build_delivered_message`, `deliver_to_agent` — is reused. Replaced: the Svix webhook + the two Resend API fetches + the svix-id dedup key (→ SES `messageId`).

**Security improvement:** SES computes SPF/DKIM/DMARC itself and passes verdicts in the event, so the anti-spoof gate — which was inert on Resend (no verdict surfaced) — becomes real. This closes review findings R1/R2/R3 (the whole `AGENT_EMAIL_REQUIRE_AUTH` footgun and the "loop guard may never fire" concern go away: the headers are in the raw MIME, and the verdicts are authoritative).

## 4. Outbound architecture (SES send)

- The reply must be sent **from** the subdomain address the sender used (`friday@{slug}.agents.isol8.co`) for threading + correctness. Resend would need each subdomain verified to send too → same wall → so **outbound also uses SES**.
- Add a sibling SES sender (`core/services/ses_email.py`) using `boto3.client("sesv2").send_email(Content={"Raw": {"Data": <MIME bytes>}})`, building the RFC-5322 message with stdlib `email.message.EmailMessage` (setting `From`, `To`, `Subject`, `In-Reply-To`, `References`, text+html parts). `agent_mail_reply.send_reply` calls this instead of `email_service.send_email`; **welcome/support mail stays on Resend** (`email_service` untouched).
- Backend task role gains `ses:SendRawEmail`/`ses:SendEmail` scoped to the SES identity ARN — added as a standalone `ManagedPolicy` (the inline policy is at CloudFormation's ~10 KB ceiling).
- Fixes review finding B1 as part of the rewrite: `send_reply` checks the send result and returns `not_sent` on failure.

## 5. Attachments (now: real store-and-link)

SES gives us the raw message, so attachments are actual bytes (no expiring Resend URL). The Lambda extracts each attachment from the MIME, stores it to an S3 bucket (`inbound/{messageId}/{filename}`), and includes `{filename, content_type, size, s3_url}` in the payload; the backend builds the manifest (reusing `build_attachment_manifest`, revised copy — no "expires ~1h"). Presigned GET URLs (short TTL) let the agent fetch if it chooses. This is the durable store-and-link the earlier plan deferred, delivered for free by the transport change.

## 6. The account-scoped SES stack (structural constraint)

AWS allows exactly **one active receipt rule set per account per region**, and `SetActiveReceiptRuleSet` has no CDK L2/L1 → activation is an `AwsCustomResource` (copy `bedrock-meter-stack.ts:120-157`). Dev+prod share the account, so SES receiving is modeled like bedrock-gate/meter: **one account-scoped stack, instantiated once**, with:
- Two SES domain identities: `agents.isol8.co` + `agents.dev.isol8.co` (each auto-writes DKIM CNAMEs into the Route53 zone via `ses.EmailIdentity(Identity.publicHostedZone(zone))`).
- Two wildcard MX records: `*.agents.isol8.co` + `*.agents.dev.isol8.co` → `inbound-smtp.us-east-1.amazonaws.com` (priority 10). (MX is hand-added `route53.MxRecord`; DKIM is auto.)
- One rule set, one catch-all rule (S3 + Lambda actions), activated via the custom resource.
- One S3 bucket (`S3_MANAGED` encryption; `ses.amazonaws.com` PutObject bucket policy with `aws:SourceAccount` condition).
- The routing Lambda knows both backend URLs and forwards by recipient subdomain.

## 7. Config + secrets

- Repurpose `AGENT_EMAIL_DOMAIN` → the per-env agent-mail base (set by CDK: `agents.isol8.co` / `agents.dev.isol8.co`). This is the single "email is on" switch (empty = inert, unchanged).
- New `SES_INBOUND_SECRET` (Secrets Manager `isol8/{env}/ses_ingest_secret`, minted in auth-stack) — the Lambda attaches it as a header; the backend timing-safe-compares. Mirrors the `ws_internal_secret` pattern.
- New `AGENT_EMAIL_ATTACHMENTS_BUCKET` (S3) for stored attachments.
- **Retire** `RESEND_WEBHOOK_SECRET` and the Resend-inbound path.

## 8. What's removed (cleanup)

`routers/agent_mail.py` (Resend webhook) → replaced by `routers/ses_inbound.py`. `agent_mail_inbound.fetch_received_email` + `list_received_attachments` + the Resend URL constants → deleted. `RESEND_WEBHOOK_SECRET` config → deleted. The Svix verifier (`core/crypto/svix.py`) stays (Clerk still uses the inline one; the extracted module simply loses its agent-mail caller — leave it, it's harmless and tested).

## 9. Phased plan (each a reviewed chunk)

1. **Address scheme** — `build_agent_email_address` → `{agent}@{slug}.{domain}`; delete the truncation/disambiguator logic; update `owner_facts` lookup; config comment. Backend-only, inert, small. (Deletes B6.)
2. **SES receiving infra (CDK)** — the account-scoped stack: identities, wildcard MX, S3 bucket + policy, rule set + catch-all rule + activation custom resource, the ingest secret. Biggest infra chunk. Inert until DNS/MX exist.
3. **The SES-ingest Lambda** — Python `fromAsset`, stdlib MIME parse, verdict extraction, attachment→S3, dev/prod routing, POST to backend with the shared secret. Unit-tested (moto/stub).
4. **Backend `/webhooks/ses-inbound` endpoint** — shared-secret auth, reusing the resolve/allowlist/cap/verdict/loop/deliver downstream; SES-messageId dedup. Replaces the Resend webhook.
5. **Outbound SES send** — `ses_email.py` sibling sender + `send_reply` reroute + task-role IAM; B1 fix.
6. **Attachments store-and-link** — MIME→S3 in the Lambda + manifest revise + presigned URLs.
7. **Cleanup** — delete the dead Resend-inbound code + `RESEND_WEBHOOK_SECRET`.
8. **Full-suite + infra synth + review**, then merge.

## 10. Activation (runbook, unchanged in spirit)

Verify the SES domain identities (auto-DKIM records land via CDK), publish the wildcard MX, activate the receipt rule set, set `AGENT_EMAIL_DOMAIN` + `SES_INBOUND_SECRET` per env, deploy the image with `isol8-mail`, reprovision, and **live-test**: a real inbound email delivers with the correct SES verdicts, a reply threads back, a spoofed sender is dropped, an attachment round-trips.

## 11. Scale honesty

This is a substantial build — a new account-scoped SES stack, a new Lambda, a new backend ingest path, the address-scheme change, outbound SES, attachments-to-S3, and cleanup. Roughly the size of the original inbound+outbound work. But it reuses ~80% of the existing logic, deletes more review findings than it adds, and is entirely inert until activation. The merge waits until it's built + reviewed + green.
