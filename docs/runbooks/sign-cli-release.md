# Sign a public CLI release

The public CLI (`TheLightbulbCompany/cli`) publishes an unsigned arm64 `lightbulb`. Unsigned,
macOS treats every update as a new program and asks for the Local Network permission again.
`.github/workflows/sign-cli-release.yml` (this repo, because the signing secrets live here)
signs it with our Developer ID and the hardened runtime, notarizes it, replaces the release's
`lightbulb-macos-arm64.tar.gz` and `.sha256`, and moves the sha256 in
`TheLightbulbCompany/homebrew-tap` `Formula/lightbulb.rb`.

It also signs the two Linux assets' `.sha256` (Linux and WSL `lightbulb update` refuses a release
without them) with the release key and uploads `lightbulb-linux-{arm64,x86_64}.tar.gz.sha256.sig`.
The key lives only on the self-hosted signing Mac, at
`~/Library/Application Support/lightbulb-release/release.key` (0600). Never print, copy or upload
it. Each signature is checked against the engine's `RELEASE_KEY` (pinned in the workflow) before
upload, and the job fails if the key file is missing.

## When

By itself, every hour at :23: it signs the latest CLI release when it has no Linux `.sig` yet and
is at least 15 minutes old (so `bump-brew` has pointed the formula's `url` at the tag). To sign
one now, or an older one, run it by hand:

```bash
gh workflow run sign-cli-release.yml -R TheLightbulbCompany/lightbulb -f tag=vX.Y.Z
gh run list -R TheLightbulbCompany/lightbulb --workflow sign-cli-release.yml --limit 1
gh run watch <run-id> -R TheLightbulbCompany/lightbulb --exit-status --interval 60
```

It is safe to re-run: a release already signed by team `WZX4U3C22Y` is not re-signed, and the
brew step does nothing when the formula already carries the asset's sha256. The run summary
lists the tag, the identity, the notarization status and the old and new sha256.

## Check

On a fresh download, not the runner's copy:

```bash
gh release download vX.Y.Z -R TheLightbulbCompany/cli -p 'lightbulb-macos-arm64.tar.gz*' -D /tmp/lb
cd /tmp/lb && shasum -a 256 -c lightbulb-macos-arm64.tar.gz.sha256 && tar -xzf lightbulb-macos-arm64.tar.gz
codesign -dvv lightbulb   # Authority=Developer ID Application: Prasiddha Parthsarthy (WZX4U3C22Y),
                          # TeamIdentifier=WZX4U3C22Y, flags=0x10000(runtime), a Timestamp line
codesign --verify --strict --verbose=2 lightbulb
```

`spctl -a -vv -t install` assesses installer packages, not executables, and for a bare binary
`syspolicy`/`spctl` gives no useful verdict ("does not seem to be an app"), so do not read
anything into it. A bare executable cannot be stapled either: Gatekeeper fetches the notarization
ticket online. The notarization result is in the run's log and summary.

## Secrets

| Secret | What |
|---|---|
| `CLI_RELEASE_TOKEN` | Fine-grained PAT, Contents: read and write on `TheLightbulbCompany/cli` only. Uploads the signed assets. The run fails at once if it is unset. |
| `HOMEBREW_TAP_DEPLOY_KEY` | Existing deploy key with write access to `TheLightbulbCompany/homebrew-tap` (shared with `desktop-build.yml`'s cask bump). |

It also uses the signing secrets `desktop-build.yml` already uses (`APPLE_CERTIFICATE`,
`APPLE_CERTIFICATE_PASSWORD`, `EXPO_ASC_API_KEY_BASE64`). The App Store Connect key id and
issuer are public metadata and sit in the workflow's `env:`.
