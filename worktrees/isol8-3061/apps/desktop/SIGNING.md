# Code signing + notarization

The desktop build (`.github/workflows/desktop-build.yml`) signs **both** dev
and prod tracks with the same `Developer ID Application: Prasiddha
Parthsarthy (WZX4U3C22Y)` certificate and notarizes via the Apple notary
service. This is a one-time setup — once the five secrets below are in
GitHub, the workflow builds signed + notarized DMGs. Qualifying pushes to
`main` publish dev only; production requires a manual workflow dispatch
after dev verification. Release tags are workflow outputs, not build
triggers. The `isol8-notarize` app-specific-password label is NOT renamed.

## One-time setup (run from the Mac that holds the Developer ID cert)

### 1. Export the Developer ID Application cert as a `.p12`

```bash
# Open Keychain Access → "login" keychain → Certificates → select
# "Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y)".
# Right-click → Export → choose Personal Information Exchange (.p12) →
# save as ~/Developer-ID-Application.p12 → set an export password.
#
# Confirm the identity is in your keychain first:
security find-identity -v -p codesigning | grep "Developer ID Application"
```

### 2. Base64-encode the `.p12` for transport through GitHub Actions

```bash
base64 -i ~/Developer-ID-Application.p12 | pbcopy
# The encoded blob is now on your clipboard. It must be a SINGLE LINE
# of base64 with no headers, no whitespace, no PEM markers.
```

### 3. Generate an app-specific password for notarization

Notarization uses your Apple ID with an **app-specific password** (not your
real Apple ID password — that won't work over the notary API).

1. Sign in at <https://appleid.apple.com>.
2. Sign-In and Security → App-Specific Passwords → "+" → label `isol8-notarize`.
3. Copy the generated password (format `xxxx-xxxx-xxxx-xxxx`).

### 4. Add the 5 GitHub repo secrets

From the repo root:

```bash
# 1. The base64-encoded .p12 from step 2 (still on clipboard).
pbpaste | gh secret set APPLE_CERTIFICATE --repo TheLightbulbCompany/lightbulb

# 2. The password you set when exporting the .p12 in step 1.
gh secret set APPLE_CERTIFICATE_PASSWORD --repo TheLightbulbCompany/lightbulb --body 'YOUR-P12-EXPORT-PASSWORD'

# 3. Your Apple ID email.
gh secret set APPLE_ID --repo TheLightbulbCompany/lightbulb --body 'you@example.com'

# 4. The app-specific password from step 3.
gh secret set APPLE_PASSWORD --repo TheLightbulbCompany/lightbulb --body 'xxxx-xxxx-xxxx-xxxx'

# 5. The Apple Developer team ID (already known — embedded in
#    tauri.conf.json's signingIdentity).
gh secret set APPLE_TEAM_ID --repo TheLightbulbCompany/lightbulb --body 'WZX4U3C22Y'
```

Verify the secrets landed:

```bash
gh secret list --repo TheLightbulbCompany/lightbulb | grep -E "^APPLE_"
# Expect: APPLE_CERTIFICATE, APPLE_CERTIFICATE_PASSWORD, APPLE_ID,
# APPLE_PASSWORD, APPLE_TEAM_ID — five rows.
```

### 5. Cut a fresh signed build

Merge reviewed desktop changes to `main` through normal CI. Changes to
`apps/desktop/**`, `pnpm-lock.yaml`, or `.github/workflows/desktop-build.yml`
trigger a dev build; the production job is skipped on push.

After verifying that dev build, manually run **desktop-build** from GitHub
Actions using the tested branch or tag. Dispatch builds dev again, then
production only if that dev job succeeds. There is no approval pause
between these jobs, so complete dev acceptance before dispatching.

The workflow sets the build version to `0.4.${{ github.run_number }}`;
do not bump local version files or push release tags to trigger a build.
It creates `desktop-dev-v0.4.<run_number>` (dev prerelease) and, on manual
dispatch, `desktop-v0.4.<run_number>` (production release).

Each job creates a draft release, uploads its signed/notarized DMG, then
automatically publishes the release. No manual draft-publication step is
required. The landing-page `/api/download/desktop` route picks up the
published release automatically — no frontend redeploy needed.

## What's in the secrets and why

| Secret                       | Source                  | Used for                                                                              |
| ---------------------------- | ----------------------- | ------------------------------------------------------------------------------------- |
| `APPLE_CERTIFICATE`          | `.p12` → base64         | `tauri-action` imports into a temp keychain on the runner before `codesign` runs.     |
| `APPLE_CERTIFICATE_PASSWORD` | `.p12` export password  | Decrypts the imported `.p12` so its private key is usable.                            |
| `APPLE_ID`                   | Your Apple ID email     | Notary service authentication.                                                        |
| `APPLE_PASSWORD`             | App-specific password   | Notary service authentication (Apple no longer accepts the real Apple ID password).   |
| `APPLE_TEAM_ID`              | `WZX4U3C22Y`            | Disambiguates which team's notary queue to submit to (required for multi-team users). |

## Rotating the cert

Developer ID Application certs are valid for 5 years. When the cert
expires (or you generate a fresh one), repeat steps 1, 2, and 4-secret-1.
Steps 3, 4-secret-2 through 4-secret-5 don't change.

## Reference

- `tauri-action` codesign env vars:
  <https://github.com/tauri-apps/tauri-action#code-signing>
- Apple app-specific passwords:
  <https://support.apple.com/en-us/102654>
- Apple notarization workflow:
  <https://developer.apple.com/documentation/security/notarizing_macos_software_before_distribution>
