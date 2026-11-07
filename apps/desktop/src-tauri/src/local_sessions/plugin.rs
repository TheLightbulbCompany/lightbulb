//! The `lightbulb` Claude Code plugin's mod (spec 2026-10-04-session-sharing-mod §4.4). The
//! canonical plugin is `packages/claude-plugin/` (the marketplace off-Mac users add); the app
//! compiles in the mod's files only, writes them to its state dir as a folder marketplace of the
//! same name, and installs them with the person's own `claude`. It leaves out the plugin's
//! `.mcp.json` (on a Mac the stdio `lightbulb` shim in native_mcp.rs is the server) and its
//! session-sharing skill (enrollment.rs copies it to ~/.claude/skills and ~/.codex/skills), so a
//! Mac never gets a second server or a second copy of that skill. The share skill
//! (`/lightbulb:share`) comes only from here. Never fatal: the mod is an addition to sharing,
//! not part of it.
//!
//! The plugin was `lightbulb-sessions` until 2026-10-07. The marketplace's `renames` map moves an
//! old install to `lightbulb@lightbulb` (checked against Claude Code 2.1.293: after the new files
//! land, `claude plugin list` shows only `lightbulb@lightbulb`, read from the folder).
//!
//! Checked against Claude Code 2.1.292 (2026-10-07): a repeat `plugin install` answers "already
//! installed ... it loads in place from <folder>, so edits there take effect at the next session
//! start or /reload-plugins". That holds for a terminal session only. Claude also keeps its own
//! copy at the install's `installPath` (~/.claude/plugins/cache/lightbulb/lightbulb/<version>),
//! and a session the Claude desktop app starts (entrypoint `claude-desktop`) loaded that copy: on
//! 2026-10-09 it showed the band of 2026-10-07 and had no `/lightbulb:share`. A repeat
//! `plugin install` never refreshes that copy; `plugin update` does. So the marketplace entry
//! carries `version()`, a hash of the bundled files, and after the install the app runs
//! `plugin update` when the version Claude recorded differs. A Claude session already running
//! keeps the mod it started with.

use super::*;

/// A short hash of every bundled file: it changes when, and only when, a file changes.
pub(crate) fn version() -> String {
    use sha2::Digest as _;
    let mut hash = sha2::Sha256::new();
    for (rel, text) in FILES {
        hash.update(rel.as_bytes());
        hash.update([0u8]);
        hash.update(text.as_bytes());
        hash.update([0u8]);
    }
    hash.finalize().iter().take(6).map(|b| format!("{b:02x}")).collect()
}

/// The text the app writes for `rel`: the bundled text, except that the marketplace entry gains
/// `version()`. The bundled marketplace.json stays unversioned, because the public marketplace
/// (packages/claude-plugin) lets Claude version its plugin by commit.
fn contents(rel: &str, text: &'static str) -> std::borrow::Cow<'static, str> {
    if rel != ".claude-plugin/marketplace.json" {
        return text.into();
    }
    let Ok(mut market) = serde_json::from_str::<serde_json::Value>(text) else { return text.into() };
    for entry in market["plugins"].as_array_mut().into_iter().flatten() {
        entry["version"] = version().into();
    }
    serde_json::to_string_pretty(&market).map_or(text.into(), |s| (s + "\n").into())
}

pub(crate) const FILES: &[(&str, &str)] = &[
    (".claude-plugin/marketplace.json", include_str!("../../../../../packages/claude-plugin/.claude-plugin/marketplace.json")),
    ("lightbulb/.claude-plugin/plugin.json", include_str!("../../../../../packages/claude-plugin/lightbulb/.claude-plugin/plugin.json")),
    ("lightbulb/mod/hooks.json", include_str!("../../../../../packages/claude-plugin/lightbulb/mod/hooks.json")),
    ("lightbulb/hooks/register.tsx", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/register.tsx")),
    ("lightbulb/hooks/state.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/state.ts")),
    ("lightbulb/hooks/rules.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/rules.ts")),
    ("lightbulb/types/index.d.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/types/index.d.ts")),
    ("lightbulb/skills/share/SKILL.md", include_str!("../../../../../packages/claude-plugin/lightbulb/skills/share/SKILL.md")),
];
pub(crate) const ID: &str = "lightbulb@lightbulb";

/// The file sync alone, with no `claude` call: fast, and safe to repeat.
pub(crate) fn write_files(state_dir: &Path) -> Result<PathBuf> {
    let root = state_dir.join("claude-mods");
    for (rel, text) in FILES {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().ok_or("plugin path")?).map_err(|_| "plugin directory unavailable")?;
        let text = contents(rel, *text);
        if fs::read_to_string(&path).ok().as_deref() != Some(&*text) {
            fs::write(&path, &*text).map_err(|_| "plugin write failed")?;
        }
    }
    let socket = enrollment::hook_path().with_file_name("mod.sock");
    fs::write(root.join("lightbulb/socket"), format!("{}\n", socket.display()))
        .map_err(|_| "plugin write failed")?;
    for sub in SOURCE_DIRS {
        remove_stale(&root.join("lightbulb").join(sub), &root);
    }
    Ok(root)
}

/// The folders of `lightbulb/` that hold only plugin source. Each build owns them whole: after a
/// sync they hold `FILES` and nothing an older build wrote. `.codex-plugin` is listed although the
/// app writes nothing there, so a manifest a past or future build puts there cannot outlive it.
/// Everything else at the plugin root is left alone, `lightbulb/socket` above all (the band reads
/// the mod socket's path from it).
const SOURCE_DIRS: &[&str] = &[".claude-plugin", ".codex-plugin", "hooks", "mod", "skills", "types"];

/// Removes each regular file under `dir` that is not in `FILES`, then each folder that left empty.
/// An app update that moves or drops a file must take the old one away: desktop 0.4.760 moved
/// `hooks/hooks.json` to `mod/hooks.json`, the old file stayed, and Claude Code refused a plugin
/// with two hooks modules, so no session had the band. Regular files only: a symlink is neither
/// followed nor removed, and a socket or any other kind of entry stays. Never fatal.
fn remove_stale(dir: &Path, root: &Path) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        let path = entry.path();
        // `DirEntry::file_type` does not follow a symlink.
        let Ok(kind) = entry.file_type() else { continue };
        if kind.is_dir() {
            remove_stale(&path, root);
            let _ = fs::remove_dir(&path); // fails unless empty, which is the intent
        } else if kind.is_file() && !FILES.iter().any(|(rel, _)| root.join(rel) == path) && fs::remove_file(&path).is_err() {
            transport::relay_log("lightbulb plugin: a file an older app wrote could not be removed");
        }
    }
}

fn commands(root: &Path, on: bool) -> Vec<Vec<String>> {
    let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
    if on {
        vec![s(&["plugin", "marketplace", "add", &root.display().to_string()]), s(&["plugin", "install", ID])]
    } else {
        vec![s(&["plugin", "uninstall", ID])]
    }
}

/// Whether `claude plugin list --json` shows `ID` read from the app's own folder (`root`). A
/// `lightbulb@lightbulb` read from anywhere else is the person's own install of the public plugin
/// (its hosted MCP server and skill), which turning sharing off must not remove. Only a positive
/// match counts: a list that fails or does not parse leaves the plugin in place. That costs little
/// when it is ours, because the mod shows nothing once sharing is off (the app removes the socket).
fn ours(list: &[u8], root: &Path) -> bool {
    our_entry(list, root).is_some()
}

fn our_entry(list: &[u8], root: &Path) -> Option<serde_json::Value> {
    serde_json::from_slice::<Vec<serde_json::Value>>(list).ok()?.into_iter().find(|p| {
        p["id"] == ID && p["readFromFolder"].as_str().is_some_and(|f| Path::new(f).starts_with(root))
    })
}

/// Whether Claude's cached copy of the app's plugin is older than the files the app wrote: the
/// version `claude plugin list --json` records is not `version()`.
fn stale(list: &[u8], root: &Path) -> bool {
    our_entry(list, root).is_some_and(|p| p["version"] != version().as_str())
}

/// Whether `claude plugin marketplace list --json` holds the public marketplace
/// (TheLightbulbCompany/lightbulb-plugin, as a `github` `repo` or a git `url`) under the name
/// `lightbulb`, added before the app. It holds the name, so the app's `marketplace add` cannot
/// register its folder and the public copy (no socket file, a blank band) stays in use. Turning
/// sharing on replaces it; the app serves everything it gave (the MCP server, the skill). Any
/// other `lightbulb` marketplace is not the app's to remove and is left alone.
fn public_marketplace(list: &[u8]) -> bool {
    let public = |v: &serde_json::Value| {
        v.as_str().is_some_and(|s| s.to_ascii_lowercase().contains("thelightbulbcompany/lightbulb-plugin"))
    };
    serde_json::from_slice::<Vec<serde_json::Value>>(list).is_ok_and(|list| {
        list.iter().any(|m| m["name"] == "lightbulb" && (public(&m["repo"]) || public(&m["url"])))
    })
}

/// Every step runs even if an earlier one fails: a repeat enable finds the marketplace and the
/// plugin already there, so "already added/installed" failures are expected and only logged.
fn run_all(home: PathBuf, root: &Path, cmds: Vec<Vec<String>>) {
    let Some(claude) = provider_program(&home, "claude") else { return };
    let run = |args: &[&str]| {
        std::process::Command::new(&claude).args(args).stdin(std::process::Stdio::null()).output()
    };
    for args in cmds {
        if args[1] == "marketplace"
            && run(&["plugin", "marketplace", "list", "--json"]).is_ok_and(|o| o.status.success() && public_marketplace(&o.stdout))
        {
            transport::relay_log("lightbulb plugin: replacing the public lightbulb marketplace with the app's copy");
            // User scope only, the scope the app adds to: a project's or a local declaration
            // (and the plugins it installed) is not the app's to remove.
            let _ = run(&["plugin", "marketplace", "remove", "lightbulb", "--scope", "user"]);
        }
        if args[1] == "uninstall" && !run(&["plugin", "list", "--json"]).is_ok_and(|o| o.status.success() && ours(&o.stdout, root)) {
            transport::relay_log("lightbulb plugin: left in place, it is not the app's copy");
            continue;
        }
        match run(&args.iter().map(String::as_str).collect::<Vec<_>>()) {
            Ok(out) if out.status.success() => {}
            Ok(out) => transport::relay_log(&format!("lightbulb plugin: {} exited {}", args.join(" "), out.status)),
            Err(_) => transport::relay_log(&format!("lightbulb plugin: {} did not start", args.join(" "))),
        }
        if args[1] == "install" {
            refresh(&claude, root);
        }
    }
}

/// Runs `claude plugin update` when Claude's cached copy of the app's plugin is older than the
/// files the app wrote (see the module comment). Every path that installs or finds the plugin
/// calls this after the files are written: the app's launch and `lightbulb continue`.
pub(crate) fn refresh(claude: &Path, root: &Path) {
    let run = |args: &[&str]| {
        std::process::Command::new(claude).args(args).stdin(std::process::Stdio::null()).output()
    };
    if !run(&["plugin", "list", "--json"]).is_ok_and(|o| o.status.success() && stale(&o.stdout, root)) {
        return;
    }
    match run(&["plugin", "update", ID]) {
        Ok(out) if out.status.success() => {}
        Ok(out) => transport::relay_log(&format!("lightbulb plugin: plugin update {ID} exited {}", out.status)),
        Err(_) => transport::relay_log(&format!("lightbulb plugin: plugin update {ID} did not start")),
    }
}

/// Install and remove are serialised, and only the latest request runs: a quick off-then-on (or
/// on-then-off) can never finish in the wrong order and leave the plugin in the stale state.
static LATEST: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static RUNNING: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn ticket() -> u64 {
    LATEST.fetch_add(1, std::sync::atomic::Ordering::SeqCst) + 1
}

fn superseded(mine: u64) -> bool {
    LATEST.load(std::sync::atomic::Ordering::SeqCst) != mine
}

fn latest_only(home: PathBuf, root: PathBuf, cmds: Vec<Vec<String>>) {
    let mine = ticket();
    std::thread::spawn(move || {
        let _turn = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
        if !superseded(mine) {
            run_all(home, &root, cmds);
        }
    });
}

/// For `lightbulb continue`: write the mod and install it now, in the foreground (the CLI exits
/// into Claude right after). Same files and the same two commands as `install`.
pub(crate) fn install_now(state_dir: &Path, home: &Path) -> Result<()> {
    let root = write_files(state_dir)?;
    run_all(home.to_owned(), &root, commands(&root, true));
    Ok(())
}

/// Files are written inline; the `claude` calls run on a background thread because they take
/// seconds and callers sit on the async runtime.
pub(crate) fn install(state_dir: &Path, home: &Path) -> Result<()> {
    let root = write_files(state_dir)?;
    latest_only(home.to_owned(), root.clone(), commands(&root, true));
    Ok(())
}

pub(crate) fn remove(state_dir: &Path, home: &Path) {
    let root = state_dir.join("claude-mods");
    latest_only(home.to_owned(), root.clone(), commands(&root, false));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_files_land_with_the_socket_path() {
        let dir = tempfile::tempdir().unwrap();
        let root = write_files(dir.path()).unwrap();
        assert!(root.join(".claude-plugin/marketplace.json").is_file());
        assert!(root.join("lightbulb/hooks/register.tsx").is_file());
        assert!(root.join("lightbulb/mod/hooks.json").is_file());
        let socket = std::fs::read_to_string(root.join("lightbulb/socket")).unwrap();
        assert!(socket.trim().ends_with("/mod.sock"));
    }

    #[test]
    fn plugin_a_changed_mod_file_replaces_the_one_an_older_app_wrote() {
        let dir = tempfile::tempdir().unwrap();
        let root = write_files(dir.path()).unwrap();
        let file = root.join("lightbulb/hooks/register.tsx");
        std::fs::write(&file, "an older app's mod").unwrap();
        write_files(dir.path()).unwrap();
        assert_eq!(std::fs::read_to_string(&file).unwrap(), include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/register.tsx"));
    }

    #[test]
    fn plugin_sync_removes_what_an_older_app_wrote_and_nothing_else() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("claude-mods");
        // What a Mac held after desktop 0.4.760: the hooks module at its old path, beside the new.
        let stale = root.join("lightbulb/hooks/hooks.json");
        let stale_dir = root.join("lightbulb/skills/gone");
        std::fs::create_dir_all(stale.parent().unwrap()).unwrap();
        std::fs::create_dir_all(&stale_dir).unwrap();
        std::fs::write(&stale, r#"{ "modules": ["./register.tsx"] }"#).unwrap();
        std::fs::write(stale_dir.join("SKILL.md"), "a dropped skill").unwrap();
        std::fs::write(root.join("lightbulb/.mcp.json"), "{}").unwrap();
        // Not plugin source: these stay.
        let kept = [root.join("lightbulb/notes"), root.join("lightbulb-sessions/hooks/hooks.json"), root.join("other")];
        for f in &kept {
            std::fs::create_dir_all(f.parent().unwrap()).unwrap();
            std::fs::write(f, "kept").unwrap();
        }
        let live = root.join("lightbulb/hooks/s"); // short: a socket path has ~104 bytes
        let _listener = std::os::unix::net::UnixListener::bind(&live).unwrap();
        let link = root.join("lightbulb/hooks/link");
        std::os::unix::fs::symlink(root.join("other"), &link).unwrap();

        assert_eq!(write_files(dir.path()).unwrap(), root);

        assert!(!stale.exists(), "two hooks modules: Claude Code refuses the plugin");
        assert!(!stale_dir.exists(), "a folder left empty goes too");
        for (rel, text) in FILES {
            assert_eq!(std::fs::read_to_string(root.join(rel)).unwrap(), contents(rel, *text), "{rel}");
        }
        assert!(std::fs::read_to_string(root.join("lightbulb/socket")).unwrap().trim().ends_with("/mod.sock"));
        for f in &kept {
            assert_eq!(std::fs::read_to_string(f).unwrap(), "kept", "{}", f.display());
        }
        assert!(root.join("lightbulb/.mcp.json").is_file(), "outside the source folders");
        assert!(std::fs::symlink_metadata(&live).is_ok(), "a socket is not a regular file");
        assert!(std::fs::symlink_metadata(&link).is_ok(), "a symlink is left alone");
    }

    #[test]
    fn plugin_the_marketplace_entry_carries_the_files_version() {
        let dir = tempfile::tempdir().unwrap();
        let root = write_files(dir.path()).unwrap();
        let text = std::fs::read_to_string(root.join(".claude-plugin/marketplace.json")).unwrap();
        let market: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(market["plugins"][0]["name"], "lightbulb");
        assert_eq!(market["plugins"][0]["source"], "./lightbulb");
        assert_eq!(market["plugins"][0]["version"], version().as_str());
        assert_eq!(market["renames"]["lightbulb-sessions"], "lightbulb");
        assert_eq!(version().len(), 12);
        assert_eq!(version(), version(), "stable for the same files");
    }

    /// A fake `claude` that logs its arguments and answers `plugin list --json` with `version`.
    fn run_with_listed_version(listed: &str) -> String {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let root = home.path().join("state/claude-mods");
        let bin = home.path().join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        let claude = bin.join("claude");
        std::fs::write(
            &claude,
            "#!/bin/sh\necho \"$*\" >> \"$0.log\"\n[ \"$*\" = \"plugin list --json\" ] && cat \"$0.list\"\nexit 0\n",
        )
        .unwrap();
        std::fs::set_permissions(&claude, std::fs::Permissions::from_mode(0o755)).unwrap();
        let entry = serde_json::json!([{"id": ID, "version": listed, "readFromFolder": root.join("lightbulb")}]);
        std::fs::write(bin.join("claude.list"), entry.to_string()).unwrap();
        run_all(home.path().to_owned(), &root, commands(&root, true));
        std::fs::read_to_string(bin.join("claude.log")).unwrap()
    }

    #[test]
    fn plugin_install_refreshes_claudes_copy_only_when_the_version_changed() {
        let log = run_with_listed_version("unknown");
        assert!(log.contains("plugin install lightbulb@lightbulb\n"), "{log}");
        assert!(log.ends_with("plugin list --json\nplugin update lightbulb@lightbulb\n"), "{log}");

        let log = run_with_listed_version(&version());
        assert!(log.contains("plugin install lightbulb@lightbulb\n"), "{log}");
        assert!(!log.contains("plugin update"), "{log}");
    }

    #[test]
    fn plugin_stale_only_for_the_apps_own_copy() {
        let root = Path::new("/state/claude-mods");
        let at = |f: &str, v: &str| format!(r#"[{{"id":"lightbulb@lightbulb","version":"{v}","readFromFolder":"{f}"}}]"#).into_bytes();
        assert!(stale(&at("/state/claude-mods/lightbulb", "unknown"), root));
        assert!(!stale(&at("/state/claude-mods/lightbulb", &version()), root));
        assert!(!stale(&at("/elsewhere/lightbulb", "unknown"), root), "not the app's to update");
        assert!(!stale(b"not json", root));
    }

    #[test]
    fn plugin_off_leaves_a_public_install_in_place() {
        let root = Path::new("/state/claude-mods");
        let at = |f: &str| format!(r#"[{{"id":"lightbulb@lightbulb","readFromFolder":"{f}"}}]"#).into_bytes();
        assert!(ours(&at("/state/claude-mods/lightbulb"), root));
        assert!(!ours(&at("/elsewhere/lightbulb"), root));
        assert!(!ours(br#"[{"id":"lightbulb@lightbulb","installPath":"/cache/lightbulb/abc"}]"#, root));
        assert!(!ours(b"[]", root));
        assert!(!ours(b"not json", root));
    }

    #[test]
    fn plugin_on_replaces_only_the_public_lightbulb_marketplace() {
        // Shapes as `claude plugin marketplace list --json` prints them (Claude Code 2.1.293).
        assert!(!public_marketplace(br#"[{"name":"lightbulb","source":"directory","path":"/state/claude-mods"}]"#));
        assert!(public_marketplace(br#"[{"name":"lightbulb","source":"github","repo":"TheLightbulbCompany/lightbulb-plugin"}]"#));
        assert!(public_marketplace(br#"[{"name":"lightbulb","source":"git","url":"https://github.com/TheLightbulbCompany/lightbulb-plugin.git"}]"#));
        assert!(!public_marketplace(br#"[{"name":"lightbulb","source":"directory","path":"/my/fork"}]"#));
        assert!(!public_marketplace(br#"[{"name":"lightbulb","source":"github","repo":"someone/lightbulb"}]"#));
        assert!(!public_marketplace(b"not json"));
    }

    #[test]
    fn plugin_the_app_copy_has_no_mcp_server_and_no_session_sharing_skill() {
        assert!(FILES.iter().all(|(p, _)| !p.ends_with(".mcp.json") && !p.contains("/skills/lightbulb-session-sharing/")));
        assert!(FILES.iter().any(|(p, _)| *p == "lightbulb/skills/share/SKILL.md"), "`/lightbulb:share` ships with the app");
    }

    #[test]
    fn plugin_commands_name_the_bundled_marketplace() {
        let cmds = commands(Path::new("/m"), true);
        assert_eq!(cmds[0], vec!["plugin", "marketplace", "add", "/m"]);
        assert_eq!(cmds[1], vec!["plugin", "install", "lightbulb@lightbulb"]);
        assert_eq!(commands(Path::new("/m"), false)[0], vec!["plugin", "uninstall", "lightbulb@lightbulb"]);
    }

    #[test]
    fn plugin_a_newer_request_supersedes_an_older_one() {
        let install = ticket();
        let remove = ticket();
        assert!(superseded(install), "the older install must not run after a newer remove");
        assert!(!superseded(remove));
    }

    #[test]
    fn plugin_files_list_covers_every_non_test_mod_file() {
        fn walk(dir: &Path, base: &Path, out: &mut Vec<String>) {
            for e in std::fs::read_dir(dir).unwrap() {
                let p = e.unwrap().path();
                if p.is_dir() {
                    walk(&p, base, out);
                } else if !p.file_name().unwrap().to_string_lossy().contains(".test.") {
                    let rel = p.strip_prefix(base).unwrap().to_string_lossy().into_owned();
                    // Left out on purpose: the Mac app serves MCP and the session-sharing skill itself, and the
                    // README and LICENSE are the public repo's front page and licence, and .codex-plugin/ is
                    // for Codex.
                    if rel != "lightbulb/.mcp.json"
                        && rel != "README.md"
                        && rel != "LICENSE"
                        && !rel.starts_with("lightbulb/skills/lightbulb-session-sharing/")
                        && !rel.starts_with("lightbulb/.codex-plugin/")
                    {
                        out.push(rel);
                    }
                }
            }
        }
        let base = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../packages/claude-plugin");
        let mut found = Vec::new();
        walk(&base, &base, &mut found);
        for f in &found {
            assert!(FILES.iter().any(|(rel, _)| rel == f), "packages/claude-plugin/{f} is missing from plugin::FILES");
        }
        assert_eq!(found.len(), FILES.len());
    }
}
