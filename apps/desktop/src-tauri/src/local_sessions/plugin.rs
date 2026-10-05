//! The `lightbulb-sessions` Claude Code plugin (spec 2026-10-04-session-sharing-mod §4.4). Its
//! files are compiled in, written to the app's state dir as a folder marketplace, and installed
//! with the person's own `claude`. Claude reads a folder marketplace from the folder itself, so
//! an app update rewrites the files and the next session loads them. Never fatal: the mod is an
//! addition to sharing, not part of it.

use super::*;

pub(crate) const FILES: &[(&str, &str)] = &[
    (".claude-plugin/marketplace.json", include_str!("../../mods/.claude-plugin/marketplace.json")),
    ("lightbulb-sessions/.claude-plugin/plugin.json", include_str!("../../mods/lightbulb-sessions/.claude-plugin/plugin.json")),
    ("lightbulb-sessions/hooks/hooks.json", include_str!("../../mods/lightbulb-sessions/hooks/hooks.json")),
    ("lightbulb-sessions/hooks/register.tsx", include_str!("../../mods/lightbulb-sessions/hooks/register.tsx")),
    ("lightbulb-sessions/hooks/state.ts", include_str!("../../mods/lightbulb-sessions/hooks/state.ts")),
    ("lightbulb-sessions/hooks/rules.ts", include_str!("../../mods/lightbulb-sessions/hooks/rules.ts")),
    ("lightbulb-sessions/types/index.d.ts", include_str!("../../mods/lightbulb-sessions/types/index.d.ts")),
];
const ID: &str = "lightbulb-sessions@lightbulb";

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
    fs::write(root.join("lightbulb-sessions/socket"), format!("{}\n", socket.display()))
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

/// Every step runs even if an earlier one fails: a repeat enable finds the marketplace and the
/// plugin already there, so "already added/installed" failures are expected and only logged.
fn run_all(home: PathBuf, cmds: Vec<Vec<String>>) {
    let Some(claude) = provider_program(&home, "claude") else { return };
    for args in cmds {
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

fn latest_only(home: PathBuf, cmds: Vec<Vec<String>>) {
    let mine = ticket();
    std::thread::spawn(move || {
        let _turn = RUNNING.lock().unwrap_or_else(|e| e.into_inner());
        if !superseded(mine) {
            run_all(home, cmds);
        }
    });
}

/// Files are written inline; the `claude` calls run on a background thread because they take
/// seconds and callers sit on the async runtime.
pub(crate) fn install(state_dir: &Path, home: &Path) -> Result<()> {
    let root = write_files(state_dir)?;
    latest_only(home.to_owned(), commands(&root, true));
    Ok(())
}

pub(crate) fn remove(_state_dir: &Path, home: &Path) {
    latest_only(home.to_owned(), commands(Path::new(""), false));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plugin_files_land_with_the_socket_path() {
        let dir = tempfile::tempdir().unwrap();
        let root = write_files(dir.path()).unwrap();
        assert!(root.join(".claude-plugin/marketplace.json").is_file());
        assert!(root.join("lightbulb-sessions/hooks/register.tsx").is_file());
        let socket = std::fs::read_to_string(root.join("lightbulb-sessions/socket")).unwrap();
        assert!(socket.trim().ends_with("/mod.sock"));
    }

    #[test]
    fn plugin_commands_name_the_bundled_marketplace() {
        let cmds = commands(Path::new("/m"), true);
        assert_eq!(cmds[0], vec!["plugin", "marketplace", "add", "/m"]);
        assert_eq!(cmds[1], vec!["plugin", "install", "lightbulb-sessions@lightbulb"]);
        assert_eq!(commands(Path::new("/m"), false)[0], vec!["plugin", "uninstall", "lightbulb-sessions@lightbulb"]);
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
                    out.push(p.strip_prefix(base).unwrap().to_string_lossy().into_owned());
                }
            }
        }
        let base = Path::new(env!("CARGO_MANIFEST_DIR")).join("mods");
        let mut found = Vec::new();
        walk(&base, &base, &mut found);
        for f in &found {
            assert!(FILES.iter().any(|(rel, _)| rel == f), "mods/{f} is missing from plugin::FILES");
        }
        assert_eq!(found.len(), FILES.len());
    }
}
