# Stripe "services account" flow vs Stripe's own docs (29 Oct 2026)

Scope: the per-workspace developer-only connected account Lightbulb creates for Stripe Projects (`company_stripe.create_company_account(kind="company_services")`, `create_account_session`, `projects_ready` on `origin/main` at `apps/backend/core/services/company_stripe.py`, `apps/backend/routers/company_stripe.py`, `apps/frontend/src/components/settings/CompanyStripeSection.tsx`). Sources: docs.stripe.com only, read through `stripe docs` on 2026-10-29. Tags: [P] primary (URL, fetched 2026-10-29); [U] unverified.

Abbreviations: PROV = https://docs.stripe.com/provisioning.md · CREATE = https://docs.stripe.com/api/v2/core/accounts/create.md · OBJ = https://docs.stripe.com/api/v2/core/accounts/object.md?api-version=2026-09-30.preview · CFG = https://docs.stripe.com/connect/accounts-v2/connected-account-configuration.md · EMB = https://docs.stripe.com/connect/supported-embedded-components/account-onboarding.md?platform=web · EMBG = https://docs.stripe.com/connect/embedded-onboarding.md · HOSTED = https://docs.stripe.com/connect/hosted-onboarding.md · LINKS = https://docs.stripe.com/api/v2/core/account-links/create.md · SESS = https://docs.stripe.com/api/account_sessions/create.md · CLOSE = https://docs.stripe.com/api/v2/core/accounts/close.md?api-version=2026-09-30.preview

## Headline

The developer configuration and `projects` capability exist only in the Provisioning guide. No API reference page fetched (CREATE with `?api-version=2026-09-30.preview&query=configuration`, OBJ, CLOSE, LINKS) mentions `developer` or `projects`; CREATE's `include` enum lists only `configuration.customer|merchant|recipient`, `defaults`, `future_requirements`, `identity`, `requirements` [P CREATE]. Everything below Stripe says about this account shape is therefore the guide plus generic Accounts v2 rules.

## 1. Create request shape

Guide example (PROV "Create the connected account") [P]: `contact_email`, `display_name`, `identity.country`, `identity.entity_type` ("individual|company"), `configuration.developer.capabilities.projects.requested: true`, `include: ["configuration.developer","requirements","identity"]`, header `Stripe-Version: 2026-09-30.preview`. No `dashboard`, no `defaults`. "Confirm the required API version and fields with your preview owner" [P PROV].

API ref top-level params, all optional: `account_token`, `configuration`, `contact_email`, `contact_phone`, `dashboard`, `defaults`, `display_name`, `identity`, `include`, `metadata` [P CREATE].

`dashboard`: "This will depend on which configurations are enabled" [P CREATE]; "You must set the dashboard property value … when you add the Merchant configuration" and when requesting recipient `stripe_transfers` [P CFG "Set Dashboard access"]. Nothing documents setting it on any other configuration; error `capability_not_available_for_dashboard_type` exists [P CREATE]. Stripe's live refusal ("cannot set dashboard unless … merchant or … recipient with stripe_balance.stripe_transfers") matches CFG.

`defaults.responsibilities`: "You must define defaults.responsibilities properties when you add the Merchant configuration" [P CFG "Set responsibilities"]; error `unsupported_field_for_configs`: "Cannot set responsibilities on the current configurations." [P CREATE]. Consistent with PR #4478 dropping both.

Lightbulb sends: `display_name`, `identity.country` (no `entity_type`), `configuration.developer…projects`, `defaults.currency` (extra), `metadata` (extra, allowed), `contact_email` when known, `include` as in the guide, preview version. Matches the guide except `entity_type` omitted and `defaults.currency` added.

## 2. Developer configuration and `projects`

"use Accounts v2, enable the developer configuration, and request the projects capability" [P PROV]. Readiness: "Check the DeveloperConfig's Projects capability and outstanding requirements" [P PROV]. Requirement model: generic v2 `requirements.entries` (section 5); no developer-specific list anywhere. The v2 Required verification information page has no developer/projects entries [P https://docs.stripe.com/connect/required-verification-information.md?accounts-namespace=v2].

Separate onboarding path: none documented. The guide reuses generic Account Links / embedded onboarding and defers to the cohort: "The approved connected account configuration and onboarding path are defined" (preview-owner checklist); "Use the Accounts API configuration and hosted onboarding flow that your cohort explicitly approves" [P PROV "Enroll in the private preview"].

## 3. Hosted vs embedded

PROV's primary section is "Send the user through hosted onboarding" (`POST /v2/core/account_links`, `use_case.type=account_onboarding`, `collection_options.fields=currently_due`, `account_update` "For subsequent corrections") [P]. Embedded is the "alternative": "For accounts that your preview owner confirms are compatible, you can embed the account onboarding component" [P PROV]. After either: "Neither the Account Link redirect nor the component's exit callback confirms that onboarding is complete" [P PROV]. HOSTED's return URL: "It doesn't mean that all information has been collected" [P].

LINKS `use_case.account_onboarding`: `refresh_url` (required), `return_url`, `collection_options.fields` (`currently_due` default | `eventually_due`), `collection_options.future_requirements` (`include|omit`) [P LINKS]. No `configurations` param in either GA or preview ref; it appears only in the response [P LINKS]. Error `account_dashboard_not_supported`: "The account does not have access to the Dashboard" [P LINKS] — whether a `dashboard: null` developer-only account trips this is undocumented [U].

Lightbulb inverts the order: embedded whenever a publishable key exists, hosted link as fallback (`CompanyStripeSection.tsx:210-236`).

## 4. The embedded component

Props/methods [P EMB]: `setCollectionOptions({fields: 'currently_due'|'eventually_due', futureRequirements: 'include'|'omit', requirements: {only|exclude: string[]}})`, `setOnExit`, `setOnStepChange` ("only for analytics"), `setFullTermsOfServiceUrl`, `setRecipientTermsOfServiceUrl`, `setPrivacyPolicyUrl`, `setSkipTermsOfServiceCollection` (deprecated). "it always collects currently_due requirements" [P EMB]. What it collects: "uses the Accounts API to read requirements and generate an onboarding form" [P EMB]; `only` "prevents account onboarding from presenting the standard final summary step" and exits immediately when satisfied [P EMB]. Session features: `external_account_collection`, `disable_stripe_user_authentication` (only when `controller.requirement_collection` is `application`) [P SESS].

Customisation documented: appearance variables (colours, fonts, borders) and `locale` [P https://docs.stripe.com/connect/embedded-appearance-options.md; https://docs.stripe.com/connect/get-started-connect-embedded-components.md?platform=web]; "a themeable onboarding UI with limited Stripe branding" [P EMBG]. Policy links/ToS handling are "For connected accounts where the platform is responsible for requirement collection" only [P EMB, EMBG]. Lightbulb's account is Stripe-collected: "Your platform is responsible only when you set … losses_collector to application and dashboard to none" [P CFG]. Hosted form branding: "customize the visual appearance of the form with your brand's name, color, and icon" in Connect settings [P HOSTED]. "Connect platforms … can't configure their own branding using the Accounts API" [P https://docs.stripe.com/get-started/account/branding.md]. No "configuration-specific" wording option is documented for either surface.

## 5. Requirements

`requirements.entries[]` [P OBJ `?query=requirements`]: `awaiting_action_from` — `user`: "Integrator should take action."; `stripe`: "Integrator should do nothing."; `description` (string); `errors[]`; `impact.restricts_capabilities[] {capability, configuration, deadline}`; `minimum_deadline.status` `currently_due|eventually_due|past_due`; `requested_reasons[].code` `routine_onboarding|routine_verification`; `reference {inquiry|resource|person}`; `requirements.summary.minimum_deadline`.

Which entries apply to developer/projects: undocumented [U]; the eight `from: user` entries Lightbulb observed are Lightbulb's data, not Stripe's. `contact_email`: "The primary contact email address for the Account." [P CREATE]; "Setting contact_email alone doesn't establish the account owner's identity or satisfy verification requirements." [P PROV]. Email confirmation: "Account emails verify an account's information, such as additions or changes to an email address" [P https://docs.stripe.com/connect/embedded-comms.md]. Whether the `contact_email` requirement clears via API or needs the user's confirmation is not stated anywhere fetched [U]; Lightbulb's own observation (requirement still present after passing `contact_email` on create) points to user confirmation [U].

## 6. The copy question

"Add information to start accepting money / … uses Stripe to help you receive payments…" appears in no fetched page; `stripe docs search` on the phrase returns nothing [P, searched 2026-10-29]. Whether it is the component's fixed default is undocumented [U]. The "isol8, Inc." token is the platform brand name from Connect settings per HOSTED's branding rule [P HOSTED]; that it is the same source for the embedded heading is inference [U].

Documented levers and why none fits: appearance/locale (visual only) [P]; ToS/privacy URLs (platform-collected accounts only) [P EMB]; `collectionOptions.requirements.only` (filters fields, skips the summary, does not restyle headings) [P EMB]; `use_case` has only `account_onboarding|account_update` [P LINKS]; no alternative component for a non-payments account [P EMB index]. Conclusion: no documented way to change or avoid the payments wording for a developer-only account.

Question for the preview owner: "Our connected accounts carry only `configuration.developer` (`projects`), never merchant/recipient. Both the `account-onboarding` embedded component and the hosted Account Link open with 'Add information to start accepting money' / '<platform> uses Stripe to help you receive payments'. Is there a configuration-aware copy variant, an Account Session feature, a collection option, or a Connect setting that suppresses payments wording for developer-only accounts — and if not, is it planned for the 2026-09-30.preview cohort?"

## 7. Provisioning-guide instructions vs code

Create the connected account [P PROV]:
- One account per durable tenant (workspace) — followed (one per billing row).
- Accounts v2 + developer configuration + `projects` requested — followed.
- Confirm version and fields with preview owner — unverifiable.
- Example fields incl. `identity.entity_type` — not followed (`entity_type` omitted; `defaults.currency` added).
- `Stripe-Version: 2026-09-30.preview` — followed (`PROVISIONING_VERSION`).
- Store the account ID on the tenant record — followed (`company_stripe_projects_account_id`).
- `contact_email` alone is not verification — followed (onboarding still run).
- "If your platform already uses Accounts v1 … confirm … with your preview owner" — unverifiable (main binding may be v1 OAuth; services account sits beside it).

Hosted onboarding [P PROV]:
- `account_links` with `account_onboarding`, refresh/return URLs, `fields: currently_due` — followed (fallback path).
- "If it expires, create another link" / refresh handler "creates a new Account Link with the same intent" — partly: `?stripe=refresh` lands on Settings with no auto-mint; the user clicks again (`ConnectionsPanel.tsx:111`).
- `account_update` for corrections — not followed for the services target (always `account_onboarding`); note HOSTED restricts `account_update` to platform-collected accounts [P], so Stripe-collected accounts may not be eligible anyway [U].
- Authenticate refresh and return handlers — followed (app-authenticated Settings page) [U: route guard not re-read].
- Continue to readiness check after return — followed (`mutate()` → `/status` → `projects_ready`).

Embedded alternative [P PROV]:
- Only for accounts the preview owner confirms compatible — unverifiable.
- `POST /v1/account_sessions` with `components.account_onboarding.enabled=true` — followed.
- Authenticate user; resolve account from verified tenant mapping — followed (`_gate_admin`, billing row).
- Restricted key with Account Sessions write — not followed (full `STRIPE_SECRET_KEY`).
- Key in server env/secrets manager — followed.
- No `Stripe-Context`/`Stripe-Account` header — followed.
- Return only `client_secret` to the authorised user — followed (plus the public key).
- HTTPS, response not cached, secret not logged — HTTPS followed; no `Cache-Control: no-store` anywhere in the backend — not followed; not logged — followed.
- New session per `fetchClientSecret`, re-verifying authorisation — followed.
- Initialise Connect.js with publishable key + `fetchClientSecret`; CSRF on endpoint — followed; CSRF unverifiable (bearer API).
- `setOnExit` → status page running readiness — followed in spirit (`onExit` → `mutate()`).
- Exit is not proof of completion — followed.

Check account readiness [P PROV]:
- `GET /v2/core/accounts/{id}?include=configuration.developer&include=requirements` — followed.
- Check Projects capability and outstanding requirements — partly: `projects_ready` checks `status == "active"` only and deliberately ignores `requirements` (`company_stripe.py:201`); the workflow then reads `/v2/provisioning/eligibility` `requirements` (`provisioning_workflow.py:413-421`), a different object.
- Provision only after capability active and onboarding resolved — followed at the workflow gate.
- Events as a prompt, not truth — n/a (none used).

## 8. Dashboard and closing

`dashboard` is nullable; the guide's example omits it, so a developer-only account has `dashboard: null` [P CREATE, OBJ]. Documented values: `none` — "can't access either Stripe Dashboard. Your platform must provide all Stripe-related functionality"; `full`; `express` [P CFG]. "Regardless of the dashboard setting, your platform can use Connect embedded components" [P CFG]. Setting it becomes mandatory only when merchant or recipient `stripe_transfers` is added [P CFG]. How the holder of a developer-only, dashboard-less account later sees or leaves the account is undocumented [U]; Stripe Projects CLI's "No separate dashboards" refers to provider dashboards [P https://docs.stripe.com/projects.md].

Closing: `POST /v2/core/accounts/{id}/close` "Removes access to the Account and its associated resources"; "All configurations on the Account must be passed in"; `applied_configurations` enum is `customer|merchant|money_manager|recipient` — no `developer` [P CLOSE]. Closing a developer-only account is therefore undocumented [U]. Lightbulb has no close or unbind path for the services account at all (`disconnect` touches only the main binding).

## Deviations summary

| # | Lightbulb | Stripe doc | Severity |
|---|---|---|---|
| 1 | Readiness = capability `active` only | "Projects capability and outstanding requirements" [P PROV] | Medium |
| 2 | Embedded default, hosted fallback | Hosted primary; embedded "for accounts that your preview owner confirms are compatible" [P PROV] | Low [U] |
| 3 | Account Session with secret key | "your platform's restricted API key … write access to Account Sessions" [P PROV] | Low |
| 4 | No `identity.entity_type`; extra `defaults.currency` | Example sends `entity_type`; no `defaults` [P PROV] | Low |
| 5 | Refresh URL needs a click | "The refresh handler creates a new Account Link" [P PROV] | Low |
| 6 | No `Cache-Control: no-store` on `/account-session` | "make sure the response isn't cached" [P PROV] | Low |
| 7 | Services account never uses `account_update` | "For subsequent corrections, use account_update" [P PROV] | Info [U] |
| 8 | No close/unbind for the services account | Close endpoint exists; `developer` not in its enum [P CLOSE] | Info [U] |
| 9 | Return banner says "onboarding finished" | Return "doesn't mean that all information has been collected" [P HOSTED] | Cosmetic |
