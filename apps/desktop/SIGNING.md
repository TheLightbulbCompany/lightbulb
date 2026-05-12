# Code signing + notarization

The desktop build (`.github/workflows/desktop-build.yml`) signs **both** dev
and prod tracks with the same `Developer ID Application: Prasiddha
Parthsarthy (WZX4U3C22Y)` certificate and notarizes via the Apple notary
service. This is a one-time setup — once the five secrets below are in
GitHub, every future `desktop-dev-v*` or `desktop-v*` tag produces a clean
signed + notarized DMG that opens without the "Isol8 is damaged" Gatekeeper
warning.

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
pbpaste | gh secret set APPLE_CERTIFICATE --repo Isol8AI/isol8

# 2. The password you set when exporting the .p12 in step 1.
gh secret set APPLE_CERTIFICATE_PASSWORD --repo Isol8AI/isol8 --body 'YOUR-P12-EXPORT-PASSWORD'

# 3. Your Apple ID email.
gh secret set APPLE_ID --repo Isol8AI/isol8 --body 'you@example.com'

# 4. The app-specific password from step 3.
gh secret set APPLE_PASSWORD --repo Isol8AI/isol8 --body 'xxxx-xxxx-xxxx-xxxx'

# 5. The Apple Developer team ID (already known — embedded in
#    tauri.conf.json's signingIdentity).
gh secret set APPLE_TEAM_ID --repo Isol8AI/isol8 --body 'WZX4U3C22Y'
```

Verify the secrets landed:

```bash
gh secret list --repo Isol8AI/isol8 | grep -E "^APPLE_"
# Expect: APPLE_CERTIFICATE, APPLE_CERTIFICATE_PASSWORD, APPLE_ID,
# APPLE_PASSWORD, APPLE_TEAM_ID — five rows.
```

### 5. Cut a fresh signed build

```bash
# Bump version in apps/desktop/src-tauri/tauri.conf.json + Cargo.toml,
# commit, then:
git tag desktop-dev-v0.2.7        # signed + notarized dev DMG
git tag desktop-v0.1.0            # signed + notarized prod DMG (first ever)
git push origin --tags
```

The workflow creates a **draft** release with the DMG attached. Approve
+ publish from the GitHub Releases UI (or `gh release edit <tag>
--draft=false`). The landing-page `/api/download/desktop` route picks
up the published release automatically — no frontend redeploy needed.

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
