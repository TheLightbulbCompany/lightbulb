# Source of truth: TheLightbulbCompany/lightbulb packaging/homebrew-tap/Casks/lightbulb.rb.
# desktop-build.yml writes `version` and `sha256` into the tap on every desktop release.
cask "lightbulb" do
  version "0.4.896"
  sha256 "a3d4953ad31845332b0118417d68f45ee9c80244369739742dd5de5dd49b49a5"

  # Always the latest signed build: the repo's releases are private, so the site proxies them.
  # The tap is bumped minutes after each release, so `version` and `sha256` follow it.
  url "https://thelightbulb.company/api/download/desktop?arch=aarch64&kind=bundle"
  name "Lightbulb"
  desc "Share Claude Code, Codex, Cursor and Grok CLI sessions with your Lightbulb workspace"
  homepage "https://thelightbulb.company/docs/agent-network-quickstart"

  # The app updates itself (Tauri updater); `brew upgrade` is not needed to stay current.
  auto_updates true
  depends_on arch: :arm64

  app "Lightbulb.app"
  # The CLI is the app's own binary: macOS notifications need the signed bundle around it.
  binary "#{appdir}/Lightbulb.app/Contents/MacOS/lightbulb-desktop", target: "lightbulb"

  uninstall launchctl: "company.thelightbulb.serve",
            delete:    "~/Library/LaunchAgents/company.thelightbulb.serve.plist"

  zap trash: [
    "~/Library/Application Support/company.thelightbulb.desktop",
    "~/Library/Caches/company.thelightbulb.desktop",
    "~/Library/Preferences/company.thelightbulb.desktop.plist",
  ]

  caveats <<~EOS
    Connect this Mac to your workspace:
      lightbulb join

    Before uninstalling, run `lightbulb leave`: it stops the background service and
    removes the shell block. What else `join` edits is listed at #{homepage}.
  EOS
end
