//! The network engine (`lightbulb`, TheLightbulbCompany/cli): one per computer, and it serves
//! the one `lightbulb` MCP server. The app bundles one and runs its `setup` only when no setup
//! has finished yet or the last one was an older version. That `setup` copies it to
//! `~/.lightbulb/connect/bin/lightbulb`, wires the agents and loads its launchd job, stopping the
//! daemon that ran before, so there is never a second one. Not a Tauri command.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// A setup that has not finished by then is stopped: the launch goes on without it.
const SETUP_LIMIT: Duration = Duration::from_secs(60);

/// At launch: installs the bundled engine when needed. Logs; never fails the launch.
pub fn ensure() {
    let (Some(home), Ok(exe)) = (std::env::var_os("HOME").map(PathBuf::from), std::env::current_exe()) else {
        return;
    };
    let Some(bundled) = bundled(&exe) else {
        return crate::log("[engine] no engine is bundled beside this build");
    };
    match ensure_at(&home, &bundled, SETUP_LIMIT) {
        Ok(true) => crate::log("[engine] installed the bundled engine"),
        Ok(false) => {}
        Err(e) => crate::log(&format!("[engine] {e}")),
    }
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

/// True when it ran the bundled engine's `setup`. The engine writes `setup-version` in its state
/// dir only when a setup succeeds, so a failed one is tried again at the next launch. A newer
/// install, or one whose version this build cannot read, is left alone.
pub(crate) fn ensure_at(home: &Path, bundled: &Path, limit: Duration) -> Result<bool, String> {
    let out = Command::new(bundled).arg("version").env("HOME", home).output().map_err(|e| format!("version: {e}"))?;
    let ours = core(&String::from_utf8_lossy(&out.stdout)).ok_or("the bundled engine does not run")?;
    let state = home.join(".lightbulb/connect");
    let installed = std::fs::read_to_string(state.join("setup-version")).ok().filter(|_| state.join("bin/lightbulb").is_file());
    if let Some(text) = installed {
        match core(&text) {
            Some(v) if v < ours => {}
            _ => return Ok(false),
        }
    }
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
            return if status.success() { Ok(true) } else { Err(format!("setup exited {status}")) };
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("setup did not finish in {} s; stopped", limit.as_secs()));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
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

    fn installed(home: &Path, setup_version: &str) {
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
            installed(home.path(), version);
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
        installed(home.path(), "0.5.9");
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
    fn the_bundled_engine_is_found_in_the_app_or_beside_a_dev_build() {
        let app = tempfile::tempdir().unwrap();
        let packaged = app.path().join("Lightbulb.app/Contents/Resources/bin/lightbulb");
        fake(&packaged, "0.6.0", "");
        assert_eq!(bundled(&app.path().join("Lightbulb.app/Contents/MacOS/lightbulb-desktop")), Some(packaged));
        assert_eq!(bundled(&app.path().join("elsewhere/lightbulb-desktop")), None);
    }
}
