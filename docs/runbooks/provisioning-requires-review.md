# provisioning — clearing a `requires_review` workflow

A provisioning workflow (`lightbulb-{env}-provisioning-workflows`) in
`requires_review` is a row **a human has to decide about**. Nothing retries it:
the driver stops, the owner's Connections card says "this one needs a look from
Lightbulb", and Friday is told to stop asking (`GET …/requests/{id}` returns the
state and the door refuses a re-ask with 409 `needs_review_first`).

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
| `uncertain_write` | a `POST` to create a project, an account request or a resource timed out or 5xx'd | **YES — this is the one the rule is for** |
| `payment_method_required` | the create was refused because the account has no payment profile | no |
| `unrenderable_schema` | the provider asked for a form shape our constrained renderer cannot draw | no |
| `credentials_not_found` | the resource reported `complete`, but `environment_info` had no entry for its id | the resource exists |
| `poll_deadline_exceeded` | the resource stayed `pending` past the 15-minute deadline | the resource exists |
| `ambiguous_provider_connection` | more than one `active` connection for the provider; the write API cannot pick | no |
| `no_company` | a later-leg card could not be filed — the owner has no Paperclip board | ids may exist |
| `driver_steps_exceeded` / `driver_wall_exceeded` | the driver gave up: a logic loop, or a row older than 8 days | depends on the state it stopped in |
| a `WorkflowRefused` code (`no_company_stripe_account`, `not_eligible`, `requirements_pending`, `service_unavailable`, …) | the state machine hit a door that will stay shut — usually the company's Stripe account was disconnected | ids may exist |

## Step 1 — read the row

```bash
aws dynamodb get-item --profile isol8-admin \
  --table-name lightbulb-prod-provisioning-workflows \
  --key '{"workflow_id":{"S":"pwf_…"}}'
```

What matters: `owner_id`, `state`, `error_code`, `provider_id`,
`service_ref`, `resource_name`, `account_request_id`, `resource_id`, and the
owner's `company_stripe_account_id` / `company_stripe_project_id` (on the
BILLING row, `lightbulb-prod-billing-accounts`).

## Step 2 — ask Stripe what exists

All three carry the same two headers. The account id is the owner's
**connected** account; `unsafe-development` is the preview pin.

```bash
CTX=acct_…            # billing row: company_stripe_account_id
H=(-H "Authorization: Bearer $STRIPE_SECRET_KEY"
   -H "Stripe-Context: $CTX"
   -H "Stripe-Version: unsafe-development")

# Every resource in the company's project — match on the row's `resource_name`,
# which is unique per workflow (`{service_id}-{last 6 of workflow_id}`).
curl -sS "${H[@]}" "https://api.stripe.com/v2/provisioning/resources?project=proj_…"

# One account request, when the row carries an id.
curl -sS "${H[@]}" "https://api.stripe.com/v2/provisioning/account_requests/acr_…"

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
  --update-expression 'SET #s = :s, updated_at = :t REMOVE sweep_key, lease_owner, lease_until' \
  --condition-expression '#s = :from' \
  --expression-attribute-names '{"#s":"state"}' \
  --expression-attribute-values '{":s":{"S":"cancelled"},":from":{"S":"requires_review"},":t":{"S":"2026-08-30T00:00:00+00:00"}}'
```

`cancelled` and not `failed`: it is a withdrawn ask, and `cancelled` is the
state the skill tells Friday she may ask again from.

**The resource DOES exist** → hand it back to the loop by writing the id and
re-opening the row. `sweep_key` must go back on or nothing will ever drive it:

```bash
aws dynamodb update-item --profile isol8-admin \
  --table-name lightbulb-prod-provisioning-workflows \
  --key '{"workflow_id":{"S":"pwf_…"}}' \
  --update-expression 'SET #s = :s, resource_id = :r, sweep_key = :open, updated_at = :t' \
  --condition-expression '#s = :from' \
  --expression-attribute-names '{"#s":"state"}' \
  --expression-attribute-values '{":s":{"S":"provisioning"},":r":{"S":"res_…"},":open":{"S":"open"},":from":{"S":"requires_review"},":t":{"S":"2026-08-30T00:00:00+00:00"}}'
```

The next backend restart's sweep picks it up (`resume_open_workflows`); it polls
the resource, binds the credentials and goes `active`. To avoid waiting for a
deploy, roll the backend service **through the admin UI**.

An `account_request_id` that turned out to exist goes back the same way, with
`account_request_id` set and the state `connecting_provider`.

## Step 4 — tell the owner

Whatever you did, say so: they have a card in Settings → Connections that has
been sitting there, and if a resource exists they are being billed for it. A
resource nobody wants is removed by the OWNER, from Settings → Connections →
Remove (typed confirmation) — never by an operator, and never by deleting our
row, which only makes us forget about infrastructure that keeps charging them.
