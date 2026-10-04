# Code signing + notarization

The desktop build (`.github/workflows/desktop-build.yml`) builds prod only
(there is no dev build since 2026-10-04) and signs it with the `Developer ID Application: Prasiddha
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

Merge reviewed desktop changes to `main` through normal CI (pull requests run
the Rust tests). Then manually run **desktop-build** from GitHub Actions
(`gh workflow run desktop-build.yml --ref main`). It builds, signs, notarizes
and publishes the prod app only. There is no dev build: test on prod, or build
and run the app locally against the dev API.
