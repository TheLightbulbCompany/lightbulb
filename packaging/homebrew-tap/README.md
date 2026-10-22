# TheLightbulbCompany/homebrew-tap

The contents of the public tap repo `TheLightbulbCompany/homebrew-tap`. The repo does not
exist yet: creating it is an owner decision. This folder is its source of truth.

```sh
brew install --cask thelightbulbcompany/tap/lightbulb
lightbulb join
```

## Set it up (once)

1. Create the public repo `TheLightbulbCompany/homebrew-tap` and copy this folder's
   contents into it (`Casks/lightbulb.rb`, this README).
2. Make a fine-grained token with **Contents: read and write** on that repo only, and save it
   as the `HOMEBREW_TAP_TOKEN` Actions secret on `TheLightbulbCompany/lightbulb`.

After that, every `desktop-build.yml` release writes the new `version` and `sha256` into
`Casks/lightbulb.rb` here (step "Bump the Homebrew tap"). Until the secret exists the step
prints a notice and skips.

## Notes

- The cask downloads from `https://thelightbulb.company/api/download/desktop?kind=bundle`,
  the site's proxy for the private releases. It always serves the latest build, so between a
  release and its tap bump (minutes) a fresh `brew install` fails its checksum; rerun it.
- `auto_updates true`: the app updates itself, so `brew upgrade` is not needed.
- The `lightbulb` command is the app's own binary (`binary` stanza), because macOS shows
  its Allow/Decline notifications only for a signed app bundle.
