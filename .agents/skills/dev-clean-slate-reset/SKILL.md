---
name: dev-clean-slate-reset
description: Wipe one Lightbulb dev test person (runtime, S3 profiles, DynamoDB rows, Stripe, Metronome, Clerk user) for fresh end-to-end testing. Use when the user asks to reset dev, wipe dev state, or clean-slate a dev account.
---

# Clean-Slate Dev Reset

The tenant is one person: the Clerk user id (`user_...`). One wipe per dev test
person; there is no shared state to clear. Requires AWS SSO
(`aws sso login --profile isol8-admin`) only for the final check.

Both doors below run the same code, `core/services/account_wipe.py`.

## Option A: the admin wipe (platform admin only)

Call it as a platform admin against `https://api-dev.thelightbulb.company/api/v1`
(`routers/admin.py`):

1. `POST /admin/users/{user_id}/wipe/preview` lists everything that would go. It deletes nothing.
2. `POST /admin/users/{user_id}/wipe` with body `{"confirm_email": "<one of the account's emails>"}`
   runs the teardown. A wrong email is a 400 `email_mismatch` and nothing is deleted.

The response report says what each step did. A failed step does not stop the
others; re-run the wipe until no step reports `failed`.

## Option B: delete the Clerk user

Deleting the user in the dev Clerk dashboard fires the `user.deleted` webhook,
which runs the same account wipe (`routers/webhooks.py`).

## Check nothing is left

No rows should remain for that user id in the dev containers or billing
tables (both are keyed on `owner_id`, which is the user id):

```bash
export AWS_PROFILE=isol8-admin AWS_REGION=us-east-1
USER_ID=user_xxx
for t in lightbulb-dev-containers lightbulb-dev-billing-accounts; do
  aws dynamodb get-item --table-name "$t" --key "{\"owner_id\":{\"S\":\"$USER_ID\"}}"
done   # each prints nothing when the row is gone
```

Dev Clerk is shared with prod previews: never use a production email for a test person.
