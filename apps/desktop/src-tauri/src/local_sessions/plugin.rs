//! The `lightbulb` Claude Code plugin's mod (spec 2026-10-04-session-sharing-mod §4.4). The
//! canonical plugin is `packages/claude-plugin/` (the marketplace off-Mac users add); the app
//! compiles in the mod's files only, writes them to its state dir as a folder marketplace of the
//! same name, and installs them with the person's own `claude`. It leaves out the plugin's
//! `.mcp.json` (on a Mac the stdio `lightbulb` shim in native_mcp.rs is the server) and its
//! `skills/` (enrollment.rs copies the skill to ~/.claude/skills and ~/.codex/skills), so a Mac
//! never gets a second server or a second copy of the skill. Claude reads a folder marketplace
//! from the folder itself, so an app update rewrites the files and the next session loads them.
//! Never fatal: the mod is an addition to sharing, not part of it.
//!
//! The plugin was `lightbulb-sessions` until 2026-10-07. The marketplace's `renames` map moves an
//! old install to `lightbulb@lightbulb` (checked against Claude Code 2.1.293: after the new files
//! land, `claude plugin list` shows only `lightbulb@lightbulb`, read from the folder).
//!
//! Checked against Claude Code 2.1.292 (2026-10-07): a repeat `plugin install` answers "already
//! installed ... it loads in place from <folder>, so edits there take effect at the next session
//! start or /reload-plugins". So no version bump is needed for a changed file to load. A Claude
//! session already running keeps the mod it started with.

use super::*;

pub(crate) const FILES: &[(&str, &str)] = &[
    (".claude-plugin/marketplace.json", include_str!("../../../../../packages/claude-plugin/.claude-plugin/marketplace.json")),
    ("lightbulb/.claude-plugin/plugin.json", include_str!("../../../../../packages/claude-plugin/lightbulb/.claude-plugin/plugin.json")),
    ("lightbulb/hooks/hooks.json", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/hooks.json")),
    ("lightbulb/hooks/register.tsx", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/register.tsx")),
    ("lightbulb/hooks/state.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/state.ts")),
    ("lightbulb/hooks/rules.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/hooks/rules.ts")),
    ("lightbulb/types/index.d.ts", include_str!("../../../../../packages/claude-plugin/lightbulb/types/index.d.ts")),
];
pub(crate) const ID: &str = "lightbulb@lightbulb";

fn write_files(state_dir: &Path) -> Result<PathBuf> {
    let root = state_dir.join("claude-mods");
    for (rel, text) in FILES {
        let path = root.join(rel);
        fs::create_dir_all(path.parent().ok_or("plugin path")?).map_err(|_| "plugin directory unavailable")?;
        if fs::read_to_string(&path).ok().as_deref() != Some(*text) {
            fs::write(&path, text).map_err(|_| "plugin write failed")?;
        }
    }
    let socket = enrollment::hook_path().with_file_name("mod.sock");
    fs::write(root.join("lightbulb/socket"), format!("{}\n", socket.display()))
        .map_err(|_| "plugin write failed")?;
    Ok(root)
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
    serde_json::from_slice::<Vec<serde_json::Value>>(list).is_ok_and(|list| {
        list.iter().any(|p| {
            p["id"] == ID && p["readFromFolder"].as_str().is_some_and(|f| Path::new(f).starts_with(root))
        })
    })
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
            let _ = run(&["plugin", "marketplace", "remove", "lightbulb"]);
        }
        if args[1] == "uninstall" && !run(&["plugin", "list", "--json"]).is_ok_and(|o| o.status.success() && ours(&o.stdout, root)) {
            transport::relay_log("lightbulb plugin: left in place, it is not the app's copy");
            continue;
        }
        match std::process::Command::new(&claude).args(&args).stdin(std::process::Stdio::null()).output() {
            Ok(out) if out.status.success() => {}
            Ok(out) => transport::relay_log(&format!("lightbulb plugin: {} exited {}", args.join(" "), out.status)),
            Err(_) => transport::relay_log(&format!("lightbulb plugin: {} did not start", args.join(" "))),
        }
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
    fn plugin_the_app_copy_has_no_mcp_server_and_no_skill() {
        assert!(FILES.iter().all(|(p, _)| !p.ends_with(".mcp.json") && !p.contains("/skills/")));
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
                    // Left out on purpose: the Mac app serves MCP and the skill itself, and the
                    // README is the public repo's front page.
                    if rel != "lightbulb/.mcp.json" && rel != "README.md" && !rel.starts_with("lightbulb/skills/") {
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
