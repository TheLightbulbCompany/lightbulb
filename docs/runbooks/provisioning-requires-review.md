# provisioning — clearing a `requires_review` workflow

A provisioning workflow (`lightbulb-{env}-provisioning-workflows`) in
`requires_review` is a row **a human has to decide about**. Nothing retries it,
and nothing ever will: `requires_review` is TERMINAL, so reading the row does
not advance it. The owner's Connections card says "this one needs a look from
Lightbulb", and Friday is told to stop asking (`GET …/requests/{id}` returns the
state and the door refuses a re-ask with 409 `needs_review_first`).

Nothing runs in the background on this rail at all — an OPEN workflow advances
one step each time somebody READS it (Friday's status route, the owner's page
loads and clicks). A parked row is not open, so it sits exactly as it is until
you move it out of `requires_review` in step 3 below. Until then, no read, no
restart and no deploy will touch it.

The reason it is not automatic: Stripe's Provisioning API has **no caller
idempotency contract**, so a state-changing POST whose answer we never saw may
or may not have created a billable resource on the owner's own Stripe account.
Repeating it is how an owner ends up paying for two.

> **The rule.** Confirm with a READ whether the Stripe object exists **before**
> you touch the row. Never patch a row on a guess, and never re-ask on the
> owner's behalf to "see what happens".

## What lands here

| `error_code` | what happened | is a Stripe object at risk? |
|---|---|---|
| `uncertain_write` | a `POST` to create a project, a provider connection request or a resource timed out or 5xx'd | **YES — this is the one the rule is for** |
| `payment_method_required` | the create was refused because the account has no payment profile | no |
| `unrenderable_schema` | the provider asked for a form shape our constrained renderer cannot draw | no |
| `credentials_not_found` | the resource reported `complete`, but `reveal_access_configuration` returned no configuration | the resource exists |
| `poll_deadline_exceeded` | the resource stayed `pending` past the 15-minute deadline | the resource exists |
| `ambiguous_provider_connection` | more than one `active` connection for the provider; the write API cannot pick | no |
| `no_company` | a later-leg card could not be filed — the owner has no Paperclip board | ids may exist |
| a `WorkflowRefused` code (`no_company_stripe_account`, `not_eligible`, `requirements_pending`, `service_unavailable`, …) | the state machine hit a door that will stay shut — usually the company's Stripe account was disconnected | ids may exist |

## Step 1 — read the row

```bash
aws dynamodb get-item --profile isol8-admin \
  --table-name lightbulb-prod-provisioning-workflows \
  --key '{"workflow_id":{"S":"pwf_…"}}'
```

What matters: `owner_id`, `state`, `error_code`, `provider_id`,
`service_ref`, `resource_name`, `provider_connection_request_id` (dev rows: `account_request_id`), `resource_id`, and the
owner's `company_stripe_account_id` / `company_stripe_project_id` (on the
BILLING row, `lightbulb-prod-billing-accounts`).

## Step 2 — ask Stripe what exists

All three carry the same two headers. The account id is the owner's
**connected** account; `2026-09-30.preview` is the preview pin.

```bash
CTX=acct_…            # billing row: company_stripe_account_id
H=(-H "Authorization: Bearer $STRIPE_SECRET_KEY"
   -H "Stripe-Context: $CTX"
   -H "Stripe-Version: 2026-09-30.preview")

# Every resource in the company's project — match on the row's `resource_name`,
# which is unique per workflow (`{service_id}-{last 6 of workflow_id}`).
curl -sS "${H[@]}" "https://api.stripe.com/v2/provisioning/resources?project=proj_…"

# One provider connection request, when the row carries an id.
curl -sS "${H[@]}" "https://api.stripe.com/v2/provisioning/provider_connection_requests/…"

# One resource, when the row carries an id.
curl -sS "${H[@]}" "https://api.stripe.com/v2/provisioning/resources/res_…"
```

`?project=` is omitted for an account-scoped service (the row's
`approval.scope` is not `project`).

## Step 3 — decide, then edit the row

Two outcomes, and the read above is what tells you which.

**Nothing was created** → close the row so the owner (or Friday) can ask again:

```bash
aws dynamodb update-item --profile isol8-admin \
  --table-name lightbulb-prod-provisioning-workflows \
  --key '{"workflow_id":{"S":"pwf_…"}}' \
  --update-expression 'SET #s = :s, updated_at = :t REMOVE lease_owner, lease_until' \
  --condition-expression '#s = :from' \
  --expression-attribute-names '{"#s":"state"}' \
  --expression-attribute-values '{":s":{"S":"cancelled"},":from":{"S":"requires_review"},":t":{"S":"2026-08-30T00:00:00+00:00"}}'
```

`cancelled` and not `failed`: it is a withdrawn ask, and `cancelled` is the
state the skill tells Friday she may ask again from.

**The resource DOES exist** → hand it back to the loop by writing the id and
moving the row out of `requires_review`. A non-terminal state is all it takes:

```bash
aws dynamodb update-item --profile isol8-admin \
  --table-name lightbulb-prod-provisioning-workflows \
  --key '{"workflow_id":{"S":"pwf_…"}}' \
  --update-expression 'SET #s = :s, resource_id = :r, updated_at = :t' \
  --condition-expression '#s = :from' \
  --expression-attribute-names '{"#s":"state"}' \
  --expression-attribute-values '{":s":{"S":"provisioning"},":r":{"S":"res_…"},":from":{"S":"requires_review"},":t":{"S":"2026-08-30T00:00:00+00:00"}}'
```

The next READ of the row picks it up — the owner opening Settings → Connections,
or Friday checking her request. It polls the resource, binds the credentials and
goes `active` over the following reads. No deploy and no restart is involved;
if nobody is looking, nothing moves, so tell the owner to open the page.

A `provider_connection_request_id` that turned out to exist goes back the same way, with
`provider_connection_request_id` set and the state `connecting_provider`.

## Step 4 — tell the owner

Whatever you did, say so: they have a card in Settings → Connections that has
been sitting there, and if a resource exists they are being billed for it. A
resource nobody wants is removed by the OWNER, from Settings → Connections →
Remove (typed confirmation) — never by an operator, and never by deleting our
row, which only makes us forget about infrastructure that keeps charging them.

---

# apps — clearing a held Vercel credential renewal

Stripe's Vercel token for an app lives one hour, and Stripe's reveal keeps
returning it after it dies. So an app's next deploy, poll, rollback or error read
after an idle hour rotates it through Stripe with nothing to see. That is routine
(logged at INFO). Only a rotation with no settled outcome stops and needs you.

**What you see.** The app's `error.code` is `credential_renewal_stuck`, or
`vercel_credential_refused` with "Lightbulb's team needs to look", and the log says
`vercel credential rotation: pending` or `errored` (WARNING).

The field is `credential_renewal: {state, at}` on the app's row in
`lightbulb-prod-apps`. `app_id` is the key.

| `state` | meaning | clears on its own? |
|---|---|---|
| `pending` | the POST's outcome is unknown: timeout, 5xx, 429, crash or cancel. Under 5 minutes old it is in flight and not shown as a hold | yes, once a read finds a changed token or the stored token working |
| `errored` | Stripe refused the rotation, or Vercel refused the new token twice | yes, once a read finds the stored token working |
| `needs_reveal` | rotation `complete`, new token not stored yet | yes, not a hold |
| `complete` | settled; another rotation may follow after 10 minutes | n/a |

No route clears a hold, and a held app never rotates by itself: Stripe says not to
resend while the outcome is unknown.

## To clear one

1. **Don't look for proof at Stripe.** Its docs say reading the Resource later doesn't
   confirm a rotation, and the preview has no way to fetch the operation. The app
   already reveals on each read that finds the token dead, so a hold still standing
   means Stripe is still handing out the same token.
2. **Wait until `at` is at least an hour old**, so a slow rotation can't be running.
   For `errored`, read the log line first: if Stripe refused for a reason that still
   holds (a disconnected account, say), fix that or the next rotation stops here again.
3. **Remove the field.** The app's next refused Vercel call reveals, then rotates if
   Stripe still holds the dead token. If the first rotation did land after all, the
   second replaces a token nobody holds. Compare `at` so you don't erase a newer attempt:

```bash
aws dynamodb update-item --profile isol8-admin \
  --table-name lightbulb-prod-apps \
  --key '{"app_id":{"S":"app_…"}}' \
  --update-expression 'REMOVE credential_renewal' \
  --condition-expression 'credential_renewal.#a = :at' \
  --expression-attribute-names '{"#a":"at"}' \
  --expression-attribute-values '{":at":{"S":"<the at you read>"}}'
```

Never call `rotate_credentials` by hand while a rotation is unresolved.
