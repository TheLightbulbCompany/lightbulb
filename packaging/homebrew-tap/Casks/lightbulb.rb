# Source of truth: TheLightbulbCompany/lightbulb packaging/homebrew-tap/Casks/lightbulb.rb.
# desktop-build.yml writes `version` and `sha256` into the tap on every desktop release.
cask "lightbulb" do
  version "0.4.962"
  sha256 "0aaa205029afb0f91b753055e681c9bd396bdc4a294d6feb33a5d9a7f22df885"

  # Always the latest signed build: the repo's releases are private, so the site proxies them.
  # The tap is bumped minutes after each release, so `version` and `sha256` follow it.
  url "https://thelightbulb.company/api/download/desktop?arch=aarch64&kind=bundle"
  name "Lightbulb"
  desc "Share Claude Code, Codex, Cursor, Grok CLI and OpenCode sessions with your Lightbulb workspace"
  homepage "https://thelightbulb.company/network"

  # The app updates itself (Tauri updater); `brew upgrade` is not needed to stay current.
  auto_updates true
  depends_on arch: :arm64

  app "Lightbulb.app"
  # No `binary` stanza: the `lightbulb` command is the network engine's (the app bundles it,
  # and so does the `lightbulb` formula), never this cask's.

  uninstall launchctl: "company.thelightbulb.serve",
            delete:    "~/Library/LaunchAgents/company.thelightbulb.serve.plist"

  zap trash: [
    "~/Library/Application Support/company.thelightbulb.desktop",
    "~/Library/Caches/company.thelightbulb.desktop",
    "~/Library/Preferences/company.thelightbulb.desktop.plist",
  ]

  caveats <<~EOS
    The app includes the Lightbulb network engine. Open the app once: it sets up the
    engine, and the `lightbulb` command comes from it (in ~/.local/bin).
    You do not need the `lightbulb` formula too: the app keeps the engine current.

    To take the engine out before uninstalling: lightbulb setup --remove
  EOS
end
