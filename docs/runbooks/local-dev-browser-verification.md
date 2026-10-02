# Local frontend against the development API

Use this runbook when a browser feature needs real Lightbulb dev data from a local frontend. It keeps browser requests same-origin so the deployed dev API's CORS policy does not need a local-origin exception.

## Start the frontend

Run from `apps/frontend` with the dev Clerk publishable key and secret supplied through process environment only. Retrieve the secret from `lightbulb/dev/clerk_secret_key` in AWS Secrets Manager; never write it to `.env` files, the repository, or logs.

```sh
export NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY='<dev publishable key>'
export CLERK_SECRET_KEY="$(aws secretsmanager get-secret-value \
  --profile isol8-admin --region us-east-1 \
  --secret-id lightbulb/dev/clerk_secret_key --query SecretString --output text)"
export NEXT_PUBLIC_API_URL='http://localhost:3000/api/v1'
export LOCAL_DEV_API_PROXY=1
pnpm run dev --hostname localhost --port 3000
```

The command assumes a raw-string secret. If Secrets Manager returns a JSON object, extract its `CLERK_SECRET_KEY` value in memory before starting Next.js. Do not enable shell tracing (`set -x`).

Use `localhost:3000` in the browser. The dev backend accepts `localhost:3000` as a Clerk token authorized party; another local port or `127.0.0.1` can trigger Clerk redirect behavior. `pnpm run dev` runs the DCV asset copy step before starting Next.js.

`LOCAL_DEV_API_PROXY=1` is required for the local route. In development, the frontend rewrites `/api/v1/*` to the fixed `https://api-dev.thelightbulb.company/api/v1/*` destination. The rewrite is absent when the flag is unset and in production.

## Sign in for a real dev workspace

Use an existing dedicated development Clerk user that is already an organization administrator of the browser-enabled test workspace. Verify `/v1/instance` reports `environment_type: development`, find the exact user using `/v1/users?email_address=<encoded email>` (not `email_address[]`), and verify membership in the intended dev organization. Mint a ticket using `POST /v1/sign_in_tokens` with `{"user_id":"<verified id>","expires_in_seconds":300}` and the dev secret as bearer authorization. Keep the response in process memory or a mode-0600 temporary file deleted immediately after redemption. Use a descriptive User-Agent for scripted Clerk requests.

Redeem the ticket in the loaded page:

```js
const signIn = await window.Clerk.client.signIn.create({
  strategy: 'ticket',
  ticket: '<short-lived ticket>',
});
await window.Clerk.setActive({
  session: signIn.createdSessionId,
  organization: '<test organization id>',
});
```

Do not paste the ticket or any key into source, screenshots, runbooks, or logs. Do not use production users or workspaces.

## Verify a browser feature

Open the target page with Browser focused before triggering the action. Capture browser console and network evidence, then verify:

- requests use `http://localhost:3000/api/v1/*` in the browser and receive expected authenticated dev responses through the rewrite;
- no production API, production Clerk instance, or mock response is used;
- the intended UI surface renders, including the DCV canvas/content when required;
- the action completes, the session ends cleanly, and transient retries do not create duplicate requests or panels.

Record the commit, date, browser/runtime versions, route, and redacted evidence in the change or test report. A sign-in screen, a successful HTTP response, or a returned signed URL alone is not proof that the live browser surface rendered.

## Verified result (2026-09-19)

On the browser branch based on main `4f0c6771b`, Next.js 16.2.12 and `bedrock-agentcore` 0.4.4:

- A dedicated dev user signed in at `http://localhost:3000` using a Clerk ticket.
- A fresh private agent conversation requested a read-only visit to `https://example.com` and a 30-second wait.
- With the Browser panel focused, real dev discovery changed from inactive to active at 23:34:16 UTC. The local canvas visibly displayed Chrome and Example Domain; screenshots were inspected, not merely counted.
- Discovery returned inactive at 23:34:58 UTC, the canvas unmounted, the idle notice returned, and the room had no active runs.
- StrictMode remained enabled. The decoder rewrite was necessary: without it, the worker returned HTML/404 and the canvas stayed blank even with a successful DCV connection.

Private verification captures are temporary local artifacts; do not commit console logs containing signed AWS URLs. This verifies localhost against deployed dev, not a deployment of the frontend branch.

## Desktop DCV startup regression

Test the installed app with a normal macOS launch (`open /Applications/Lightbulb.app`),
not only by executing `Contents/MacOS/lightbulb-desktop` from a terminal. On the
2026-09-21 founder reproduction, a normal launch exposed no `navigator.mediaDevices`,
whereas the terminal launch exposed a native media API and rendered successfully.
That difference concealed the failure during earlier tests.

The SDK bundled with `bedrock-agentcore@0.4.4` detects its native WebRTC-redirection
host by the presence of `window.webkit.messageHandlers.ipc`. Tauri also exposes that
handler. DCV therefore initializes the wrong native client at module evaluation,
requests microphone access and calls `mediaDevices.addEventListener`. With the
missing-media shim's plain object this throws before authentication:
`TypeError: navigator.mediaDevices.addEventListener is not a function`.
An unhandled `The webcam system is not supported.` rejection accompanies it.
Selecting display/input-only connection channels cannot prevent import-time work.

`copy-dcv-assets.mjs` excludes `window.__TAURI_INTERNALS__` from that native-host
detection in the generated ESM and UMD SDK copies. The Next alias consumes the
guarded ESM copy; the original dependency is unchanged. Generation fails if the
expected upstream detection changes, requiring a new audit on dependency upgrades.
The SDK regression test executes the real generated SDK with a Tauri IPC handler
and asserts that import does not capture local media or send DCV traffic to Tauri.

Local native A/B evidence: the instrumented Tauri executable launched through
macOS reproduced the exact exception above. Changing only the SDK detection guard
in memory, under the same normal-launch conditions with the media API still absent,
rendered a DCV canvas showing YouTube and removed both import-time errors. This is
local proof of the guard, not proof of a later production deployment. Recheck the
official installed app after promotion. Viewer startup exceptions now retain their
stage and sanitized error details in console/PostHog; signed URLs are redacted.

## Current limits

Direct browser calls from an arbitrary local origin fail the deployed dev API's CORS policy, so bypassing the proxy is not a valid test. Teams-related API surfaces may currently return `503`; record that separately from Browser/DCV results. Cookie persistence across AgentCore browser sessions is not established by this procedure and requires its own two-turn measurement.

For DCV asset failures, inspect the requested worker URL. The SDK may request `/chat/nice-dcv-web-client-sdk/...` while copied assets are served at `/nice-dcv-web-client-sdk/...`; the checked-in Next rewrite maps the former prefix to the latter. Keep StrictMode enabled and treat duplicate-connection logs separately when the canvas renders.

## Check a profile without a raw browser override

Include an existing or fresh profile whose raw `config.yaml` omits `browser.backend` in browser acceptance. A test account already carrying `browser.backend: off` can hide a migration failure: Hermes 0.21.3's Browser Use selector reads raw profile config instead of the managed overlay. It then chooses the Browser Use harness, whose unsigned AgentCore CDP connection returns 403.

The bridge's `normalize_profile_backends` projects the platform's `browser.backend: off` policy into the local raw profile before either room or native Paperclip agent construction. This preserves the configured model/provider and other profile values, and selects the released agent-browser path. The backend-owned S3 config is not rewritten by the runtime's profile save; verify the actual tool selection and live page, not an S3 backfill. The pinned-image browser smoke covers missing and conflicting raw settings.


## Browser v2 verification and rollout

Browser v2 requires both the backend/CDK change and a new Hermes image. A backend
merge alone does not roll running runtimes. Build a candidate through
`build-hermes-image.yml`, promote its dev tag in `hermes-version.json`, then use
`scripts/roll_hermes_runtimes.py` for the dedicated dev workspace. Production
promotion is separate.

On the dedicated dev account, verify this complete sequence after rollout:

1. Open Browser: no session is created by opening or polling the panel.
2. Ask an agent to browse a controlled test site. Under its working card a
   "<agent> is browsing <site>" line and a live thumbnail appear; on a wide window
   the Browser panel opens beside the chat once per tab session.
3. While the agent browses, click and type in the stream (the remote Chrome's own
   address bar is the only one). Both the person's and the agent's actions land;
   the agent waits a few seconds after the person's input and re-snapshots.
4. Sign in (or set a synthetic persistent cookie/localStorage) and stop touching the
   stream. Within ~10-20 s the bar reads "Saved <time>" without any button.
5. Click **Stop**, then Start again. Confirm a different AWS session ID bound to the
   same workspace profile restores the synthetic cookie/localStorage.
6. Verify a second workspace cannot obtain the first workspace's view.
7. Simulate a failed save. The live browser and prior checkpoint must remain and the
   next save retries; abrupt session expiry can lose changes since the last save.

Component proof completed on 2026-09-20: the released `agent-browser@0.38.1` helper
saved synthetic state to a dedicated dev S3 prefix and restored cookie plus
localStorage into a fresh profile-bound AgentCore Browser session. Both sessions,
the disposable profile, and all probe S3 objects were removed. Restore also removed deliberately stale cookies and localStorage, including an
origin whose saved storage was empty. The exact pinned
Hermes image also passed subprocess credential filtering and hook integration
checks. These component results are not a claim that the complete deployed v2 UI
sequence above has passed.

Operational details verified by that probe: disconnect the agent's automation
client before checkpointing (AWS permits one automation connection); explicitly
connect the helper before `state save`; use explicit `state load` followed by reload;
and sign S3 PUT with the same `Content-Type: application/json` the helper sends.
Never record signed URLs, browser-state JSON, cookies, or Clerk tickets in logs.


Checkpoint scope: cookies plus the localStorage origins exported by the CLI, merged
with previously saved origins. For a newly signed-in site, the autosave runs while
that site is still open once the person goes quiet. Uncheckpointed localStorage changes on a site that has already been closed
or navigated away from are not guaranteed to be captured. Full closed-origin history
collection would need a separate browser observer; it is not part of this release.
