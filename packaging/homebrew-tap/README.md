# TheLightbulbCompany/homebrew-tap

The cask in the public tap repo `TheLightbulbCompany/homebrew-tap`. This folder is its source
of truth. The tap's `Formula/lightbulb.rb` (the network engine alone) is bumped by the engine
repo's own release workflow, not from here.

```sh
brew install --cask thelightbulbcompany/tap/lightbulb   # the app (it bundles the engine)
brew install thelightbulbcompany/tap/lightbulb          # or the engine alone
```

## Set it up (once)

Done on 2026-10-22: the public repo `TheLightbulbCompany/homebrew-tap` holds this folder's
contents, and a deploy key with write access to that repo only is saved as the
`HOMEBREW_TAP_DEPLOY_KEY` Actions secret on `TheLightbulbCompany/lightbulb`. To rotate it,
generate a new ed25519 key, replace the deploy key on the tap repo and the secret, and delete
the old key.

After that, every `desktop-build.yml` release writes the new `version` and `sha256` into
`Casks/lightbulb.rb` here (step "Bump the Homebrew tap"). Until the secret exists the step
prints a notice and skips.

## Notes

- The cask downloads from `https://thelightbulb.company/api/download/desktop?kind=bundle`,
  the site's proxy for the private releases. It always serves the latest build, so between a
  release and its tap bump (minutes) a fresh `brew install` fails its checksum; rerun it.
- `auto_updates true`: the app updates itself, so `brew upgrade` is not needed.
- The cask has no `binary` stanza. The `lightbulb` command is the network engine's: the app
  bundles it and its `setup` links `~/.local/bin/lightbulb`. A cask binary would collide with
  the formula.
