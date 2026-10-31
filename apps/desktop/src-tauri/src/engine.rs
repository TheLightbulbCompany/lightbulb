//! The network engine (`lightbulb`, TheLightbulbCompany/cli): one per computer, and it serves
//! the one `lightbulb` MCP server. The app bundles one and runs its `setup` only when no setup
//! has finished yet or the last one was an older version. That `setup` copies it to
//! `~/.lightbulb/connect/bin/lightbulb`, wires the agents and loads its launchd job, stopping the
//! daemon that ran before, so there is never a second one. `engine_fix` is the one command: the
//! sidebar's Fix, which re-runs that `setup` and takes nothing from the page.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// A setup that has not finished by then is stopped: the launch goes on without it.
const SETUP_LIMIT: Duration = Duration::from_secs(60);
/// Fix's: `setup` may install a model first (up to 300 s); stopping it then leaves the service down.
const FIX_LIMIT: Duration = Duration::from_secs(360);

/// At launch: installs the bundled engine when needed. Logs; never fails the launch.
pub fn ensure() {
    let (home, bundled) = match here() {
        Ok(found) => found,
        Err(e) => return crate::log(&format!("[engine] {e}")),
    };
    let Some(_flight) = single_flight() else {
        return crate::log("[engine] a setup is already running");
    };
    match ensure_at(&home, &bundled, SETUP_LIMIT) {
        Ok(true) => crate::log("[engine] installed the bundled engine"),
        Ok(false) => {}
        Err(e) => crate::log(&format!("[engine] {e}")),
    }
}

/// Whether Fix may run `setup` now: never over a service that answers (setup restarts it), never
/// from an older app over a newer or unreadable install (setup overwrites the installed binary).
fn fix_allowed(engine_answers: bool, installed: Option<&str>, bundled: &str) -> Result<(), String> {
    if engine_answers {
        return Err("the background service is running".into());
    }
    let ours = core(bundled).ok_or("the bundled engine does not run")?;
    match installed {
        None => Ok(()),
        Some(text) => match core(text) {
            Some(v) if v <= ours => Ok(()),
            _ => Err("a newer or unreadable engine is installed".into()),
        },
    }
}

/// One setup at a time: the launch's and Fix's share it. Dropping the guard frees it.
static SETTING_UP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct Flight;

fn single_flight() -> Option<Flight> {
    use std::sync::atomic::Ordering;
    (!SETTING_UP.swap(true, Ordering::SeqCst)).then_some(Flight)
}

impl Drop for Flight {
    fn drop(&mut self) {
        SETTING_UP.store(false, std::sync::atomic::Ordering::SeqCst);
    }
}

/// (HOME, the engine bundled with the running app).
fn here() -> Result<(PathBuf, PathBuf), &'static str> {
    let home = std::env::var_os("HOME").map(PathBuf::from).ok_or("no HOME")?;
    let exe = std::env::current_exe().map_err(|_| "this app's path is unknown")?;
    Ok((home, bundled(&exe).ok_or("no engine is bundled beside this build")?))
}

/// The finished setup's version; None when there is none. Only a missing file means none: a
/// marker that cannot be read (permissions, not UTF-8, a directory) is an Err, never "absent".
fn installed(home: &Path) -> Result<Option<String>, String> {
    let state = home.join(".lightbulb/connect");
    match std::fs::read_to_string(state.join("setup-version")) {
        Ok(text) => Ok(Some(text).filter(|_| state.join("bin/lightbulb").is_file())),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(format!("setup-version: {e}")),
    }
}

fn version_of(home: &Path, bundled: &Path) -> Result<String, String> {
    let out = Command::new(bundled).arg("version").env("HOME", home).output().map_err(|e| format!("version: {e}"))?;
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Packaged: Contents/MacOS/<exe> -> Contents/Resources/bin/lightbulb. Dev: bin/ beside the exe.
fn bundled(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    [macos.parent().map(|c| c.join("Resources/bin/lightbulb")), Some(macos.join("bin/lightbulb"))]
        .into_iter()
        .flatten()
        .find(|p| p.is_file())
}

/// "0.6.0", "0.7.0-rc1" or "lightbulb 0.6.0" as its numeric core ([0, 7, 0]); None otherwise.
fn core(text: &str) -> Option<Vec<u64>> {
    let word = text.split_whitespace().last()?;
    word.split(['-', '+']).next()?.split('.').map(|n| n.parse().ok()).collect()
}

/// The sidebar's Fix: re-applies the bundled engine when `fix_allowed` says so.
#[tauri::command]
pub async fn engine_fix() -> Result<(), String> {
    let (home, bundled) = here()?;
    // The flag first, the ask inside it: no answer can go stale while another setup finishes.
    let flight = single_flight().ok_or("a setup is already running")?;
    let answers = crate::engine_client::ask("sessions", None).await.is_ok();
    tokio::task::spawn_blocking(move || {
        let _flight = flight;
        fix_allowed(answers, installed(&home)?.as_deref(), &version_of(&home, &bundled)?)?;
        fix_at(&home, &bundled, FIX_LIMIT)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Runs the bundled engine's `setup`, without asking whether it should (`ensure_at` and
/// `engine_fix` do): the same spawn, wait and limit for both.
pub(crate) fn fix_at(home: &Path, bundled: &Path, limit: Duration) -> Result<(), String> {
    let mut child = Command::new(bundled)
        .arg("setup")
        .env("HOME", home)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("setup did not start: {e}"))?;
    let deadline = Instant::now() + limit;
    loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            return if status.success() { Ok(()) } else { Err(format!("setup exited {status}")) };
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("setup did not finish in {} s; stopped", limit.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// True when it ran the bundled engine's `setup`. The engine writes `setup-version` in its state
/// dir only when a setup succeeds, so a failed one is tried again at the next launch. A newer
/// install, or one whose version this build cannot read, is left alone.
pub(crate) fn ensure_at(home: &Path, bundled: &Path, limit: Duration) -> Result<bool, String> {
    let ours = core(&version_of(home, bundled)?).ok_or("the bundled engine does not run")?;
    if let Some(text) = installed(home)? {
        match core(&text) {
            Some(v) if v < ours => {}
            _ => return Ok(false),
        }
    }
    fix_at(home, bundled, limit)?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const LONG: Duration = Duration::from_secs(30);

    /// A fake engine: answers `version`; its `setup` records itself in `<home>/setup.log`, runs
    /// `setup`, then installs itself and writes `setup-version`, as the real one does on success.
    fn fake(path: &Path, version: &str, setup: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, format!(
            "#!/bin/sh\ncase \"$1\" in\n  version) echo \"lightbulb {version}\" ;;\n  setup) echo \"setup {version}\" >> \"$HOME/setup.log\"\n    {setup}\n    mkdir -p \"$HOME/.lightbulb/connect/bin\" && cp \"$0\" \"$HOME/.lightbulb/connect/bin/lightbulb\"\n    printf {version} > \"$HOME/.lightbulb/connect/setup-version\" ;;\nesac\n"
        )).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn setups(home: &Path) -> String {
        std::fs::read_to_string(home.join("setup.log")).unwrap_or_default()
    }

    fn put_install(home: &Path, setup_version: &str) {
        fake(&home.join(".lightbulb/connect/bin/lightbulb"), "0.0.0", "");
        std::fs::write(home.join(".lightbulb/connect/setup-version"), setup_version).unwrap();
    }

    #[test]
    fn ensure_uses_a_running_installed_engine_and_starts_none() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0", "");
        // As new, newer (a pre-release counts by its numeric core), or unreadable: left alone.
        for version in ["0.6.0", "0.7.1", "0.7.0-rc1", "garbage"] {
            put_install(home.path(), version);
            assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(false), "{version}");
        }
        assert_eq!(setups(home.path()), "");
    }

    #[test]
    fn ensure_installs_the_bundled_engine_when_missing_or_older() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0", "");
        assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(true), "missing");
        assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(false), "its setup finished");
        put_install(home.path(), "0.5.9");
        assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(true), "older");
        // A binary with no finished setup (the marker is written only on success): set up again.
        std::fs::remove_file(home.path().join(".lightbulb/connect/setup-version")).unwrap();
        assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(true), "no finished setup");
        assert_eq!(setups(home.path()), "setup 0.6.0\nsetup 0.6.0\nsetup 0.6.0\n");
        // A bundled binary that does not run installs nothing.
        std::fs::write(&bundled, "").unwrap();
        assert!(ensure_at(home.path(), &bundled, LONG).is_err());
    }

    #[test]
    fn a_failed_setup_is_tried_again_and_a_stuck_one_is_stopped() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0", "exit 1");
        assert!(ensure_at(home.path(), &bundled, LONG).is_err());
        assert!(ensure_at(home.path(), &bundled, LONG).is_err(), "no marker: the next launch tries again");
        assert_eq!(setups(home.path()), "setup 0.6.0\nsetup 0.6.0\n");
        fake(&bundled, "0.6.0", "sleep 30");
        let started = Instant::now();
        assert!(ensure_at(home.path(), &bundled, Duration::from_secs(1)).unwrap_err().contains("stopped"));
        assert!(started.elapsed() < Duration::from_secs(10));
    }

    #[test]
    fn fix_runs_setup_even_when_the_install_is_current() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0", "");
        put_install(home.path(), "0.6.0");
        assert_eq!(ensure_at(home.path(), &bundled, LONG), Ok(false));
        assert_eq!(fix_at(home.path(), &bundled, LONG), Ok(()));
        assert_eq!(setups(home.path()), "setup 0.6.0\n");
        fake(&bundled, "0.6.0", "exit 1");
        assert!(fix_at(home.path(), &bundled, LONG).is_err());
    }

    #[test]
    fn fix_leaves_a_healthy_or_newer_install_alone() {
        assert!(fix_allowed(true, Some("0.6.0"), "lightbulb 0.6.0").is_err(), "a service that answers");
        assert!(fix_allowed(false, Some("0.7.0"), "lightbulb 0.6.0").is_err(), "newer");
        assert!(fix_allowed(false, Some("garbage"), "lightbulb 0.6.0").is_err(), "unreadable");
        assert!(fix_allowed(false, Some("0.6.0"), "garbage").is_err(), "bundled does not run");
        for installed in [None, Some("0.5.9"), Some("0.6.0")] {
            assert_eq!(fix_allowed(false, installed, "lightbulb 0.6.0"), Ok(()), "{installed:?}");
        }
    }

    #[test]
    fn an_install_marker_that_cannot_be_read_is_not_an_absent_one() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0", "");
        assert_eq!(installed(home.path()), Ok(None), "nothing installed");
        put_install(home.path(), "0.6.0");
        std::fs::remove_file(home.path().join(".lightbulb/connect/setup-version")).unwrap();
        // A directory where the file belongs: not NotFound, so not "absent".
        let marker = home.path().join(".lightbulb/connect/setup-version");
        std::fs::create_dir(&marker).unwrap();
        assert!(installed(home.path()).is_err());
        assert!(ensure_at(home.path(), &bundled, LONG).is_err());
        assert_eq!(setups(home.path()), "");
        std::fs::remove_dir(&marker).unwrap();
        std::fs::write(&marker, [0xff, 0xfe]).unwrap();
        assert!(installed(home.path()).is_err(), "not UTF-8");
    }

    #[test]
    fn only_one_setup_is_in_flight() {
        let first = single_flight().expect("free");
        assert!(single_flight().is_none());
        drop(first);
        assert!(single_flight().is_some());
    }

    #[test]
    fn the_bundled_engine_is_found_in_the_app_or_beside_a_dev_build() {
        let app = tempfile::tempdir().unwrap();
        let packaged = app.path().join("Lightbulb.app/Contents/Resources/bin/lightbulb");
        fake(&packaged, "0.6.0", "");
        assert_eq!(bundled(&app.path().join("Lightbulb.app/Contents/MacOS/lightbulb-desktop")), Some(packaged));
        assert_eq!(bundled(&app.path().join("elsewhere/lightbulb-desktop")), None);
    }

    #[test]
    fn the_vendored_engine_is_at_least_the_floor() {
        let script = include_str!("../scripts/vendor-sidecars.sh");
        let tag = script.lines().find_map(|line| line.strip_prefix("ENGINE_TAG=\"")?.strip_suffix('"')).unwrap();
        assert!(core(tag.trim_start_matches('v')).unwrap() >= vec![0, 8, 0], "this app reads the engine from 0.8.0 on; the script vendors {tag}");
    }
}
