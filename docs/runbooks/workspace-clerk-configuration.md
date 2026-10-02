# Workspace-only Clerk configuration

This runbook records the required Clerk organization settings for Lightbulb.
The application must host `/session-tasks/choose-organization` before membership
is required: Clerk leaves a user without an active organization in a pending
session, and that native task is what completes the session safely.

## Desired values

Apply the same values to the dev and production Clerk instances, in that order:

| Setting | Desired value |
| --- | --- |
| Organizations | enabled (`organizations.enabled = true`) |
| Personal accounts | disabled / organization membership required (`force_organization_selection = true`) |
| Automatic organization creation | disabled |
| Organization creator role | `org:admin` |
| Default member role | `org:member` |

Automatic organization creation must stay disabled. Lightbulb and Clerk must be
able to present an existing invitation before offering workspace creation.

## Read back an instance

Clerk currently splits this verification across two APIs. The authenticated
Backend API organization-settings endpoint reports organization enablement,
role defaults, and the initial Role Set. The public Frontend API (FAPI)
environment reports membership-required and automatic creation. The latter two
settings are changed in the Clerk Dashboard; Clerk's documented Backend API
PATCH schema does not accept them, so do not invent an undocumented mutation
request.

Set these shell variables to the instance being checked. Do not paste their
values into this document, tickets, or logs:

```sh
export CLERK_SECRET_KEY='<instance secret key>'
export CLERK_ISSUER='https://<instance>.clerk.accounts.dev'
```

Backend API read-back:

```sh
curl -fsS https://api.clerk.com/v1/instance/organization_settings \
  -H "Authorization: Bearer ${CLERK_SECRET_KEY}" \
  | jq '{enabled, creator_role, default_role: .domains_default_role,
         initial_role_set_key}'
```

Expected values:

```json
{
  "enabled": true,
  "creator_role": "org:admin",
  "default_role": "org:member",
  "initial_role_set_key": "role_set:default"
}
```

`domains_default_role` is Clerk's Backend API field for the Role used for new
Organization invitations and suggestions. Verify the initial Role Set agrees:

```sh
ROLE_SET_KEY="$(
  curl -fsS https://api.clerk.com/v1/instance/organization_settings \
    -H "Authorization: Bearer ${CLERK_SECRET_KEY}" \
    | jq -er '.initial_role_set_key'
)"

curl -fsS "https://api.clerk.com/v1/role_sets/${ROLE_SET_KEY}" \
  -H "Authorization: Bearer ${CLERK_SECRET_KEY}" \
  | jq '{key, creator_role: .creator_role.key,
         default_role: .default_role.key}'
```

Expected values:

```json
{
  "key": "role_set:default",
  "creator_role": "org:admin",
  "default_role": "org:member"
}
```

Public FAPI read-back (no secret header):

```sh
curl -fsS "${CLERK_ISSUER%/}/v1/environment?_clerk_js_version=5" \
  | jq '(.response // .).organization_settings
    | {
        force_organization_selection,
        automatic_organization_creation_enabled:
          ([.organization_creation_defaults.automatic_organization_creation.enabled,
            .organization_creation_defaults.enabled]
           | map(select(. != null)) | first)
      }'
```

The fallback in the query accounts for the two FAPI response shapes used by
supported Clerk clients. The required result is:

```json
{
  "force_organization_selection": true,
  "automatic_organization_creation_enabled": false
}
```

If either API omits a field or returns an unexpected shape, stop. Save the
redacted response shape, confirm the current Clerk API contract, and do not
infer that a missing value means `false`.

## Rollout order

### Development

1. Merge the application code to `main`. Do not change either Clerk instance
   first.
2. Let the automatic `main` deployment put the frontend and backend on dev
   while dev Clerk still allows optional organization membership.
3. Verify the deployed commit is live and
   `https://dev.thelightbulb.company/session-tasks/choose-organization` resolves to the
   Clerk native task route rather than redirecting to `/chat`, `/onboarding`, or
   `/admin`. Run the workspace E2E once with membership still optional.
4. In the **dev Clerk Dashboard only**, set the five desired values above.
   This Dashboard edit is the configuration mutation boundary.
5. Run both API read-backs against dev. Then rerun the consolidated workspace
   E2E and founder-test sign-up, invitation acceptance, workspace creation, and
   existing-workspace selection. Confirm the invitee activates the invited
   workspace and no second runtime or billing owner appears.

### Production

6. Continue only after dev acceptance. Dispatch the deploy workflow from
   `main` with `gh workflow run deploy.yml --ref main`; the manual dispatch
   includes production jobs. **Never dispatch this workflow from the feature
   branch.**
7. Verify the intended commit and the session-task route are live in production
   while production Clerk membership is still optional.
8. In the **production Clerk Dashboard**, apply the five desired values.
9. Run both API read-backs against production, then founder-test the same
   invitation-first and create/select paths. Keep a signed-in recovery session
   available until this check completes.

If the native task route or either read-back is wrong at any stage, stop before
the next Clerk mutation. Restore optional membership in that instance through
the Dashboard if users cannot complete the task, then diagnose the deployed
route/configuration mismatch.
