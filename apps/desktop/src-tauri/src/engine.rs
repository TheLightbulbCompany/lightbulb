//! The network engine (`lightbulb`, TheLightbulbCompany/cli): one per computer, and it serves
//! the one `lightbulb` MCP server. The app bundles one and installs it only when none is
//! installed or the installed one is older. The engine's own `setup` copies it to
//! `~/.lightbulb/connect/bin/lightbulb`, wires the agents and loads its launchd job, stopping
//! the daemon that ran before, so there is never a second one. Not a Tauri command.

use std::path::{Path, PathBuf};
use std::process::Command;

/// At launch: installs the bundled engine when needed. Logs; never fails the launch.
pub fn ensure() {
    let (Some(home), Ok(exe)) = (std::env::var_os("HOME").map(PathBuf::from), std::env::current_exe()) else {
        return;
    };
    let Some(bundled) = bundled(&exe) else {
        return crate::log("[engine] no engine is bundled beside this build");
    };
    match ensure_at(&home, &bundled) {
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

/// `lightbulb version` ("lightbulb 0.6.0") as numbers; None when it does not run.
fn version(bin: &Path, home: &Path) -> Option<Vec<u64>> {
    let out = Command::new(bin).arg("version").env("HOME", home).output().ok().filter(|o| o.status.success())?;
    let text = String::from_utf8(out.stdout).ok()?;
    text.split_whitespace().last()?.split('.').map(|n| n.parse().ok()).collect()
}

/// True when it ran the bundled engine's `setup`. An engine already installed, as new or newer,
/// is left alone: its launchd job keeps its daemon running, and nothing here starts another.
pub(crate) fn ensure_at(home: &Path, bundled: &Path) -> Result<bool, String> {
    let ours = version(bundled, home).ok_or("the bundled engine does not run")?;
    if version(&home.join(".lightbulb/connect/bin/lightbulb"), home).is_some_and(|v| v >= ours) {
        return Ok(false);
    }
    // `setup` installs itself as the canonical copy, wires the agents, and swaps the daemon.
    let out = Command::new(bundled).arg("setup").env("HOME", home).output().map_err(|e| format!("setup did not start: {e}"))?;
    if !out.status.success() {
        return Err(format!("setup exited {}", out.status));
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// A fake engine: answers `version`, and records each `setup` in `<home>/setup.log`.
    fn fake(path: &Path, version: &str) {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, format!(
            "#!/bin/sh\ncase \"$1\" in\n  version) echo \"lightbulb {version}\" ;;\n  setup) echo \"setup {version}\" >> \"$HOME/setup.log\" ;;\nesac\n"
        )).unwrap();
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn setups(home: &Path) -> String {
        std::fs::read_to_string(home.join("setup.log")).unwrap_or_default()
    }

    #[test]
    fn ensure_uses_a_running_installed_engine_and_starts_none() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0");
        for installed in ["0.6.0", "0.7.1"] {
            fake(&home.path().join(".lightbulb/connect/bin/lightbulb"), installed);
            assert_eq!(ensure_at(home.path(), &bundled), Ok(false), "{installed}");
        }
        assert_eq!(setups(home.path()), "", "an engine as new or newer is left as it is");
    }

    #[test]
    fn ensure_installs_the_bundled_engine_when_missing_or_older() {
        let home = tempfile::tempdir().unwrap();
        let bundled = home.path().join("app/bin/lightbulb");
        fake(&bundled, "0.6.0");
        assert_eq!(ensure_at(home.path(), &bundled), Ok(true), "missing");
        fake(&home.path().join(".lightbulb/connect/bin/lightbulb"), "0.5.9");
        assert_eq!(ensure_at(home.path(), &bundled), Ok(true), "older");
        assert_eq!(setups(home.path()), "setup 0.6.0\nsetup 0.6.0\n", "the bundled engine's own setup, each time");
        // A bundled binary that does not run installs nothing.
        std::fs::write(&bundled, "").unwrap();
        assert!(ensure_at(home.path(), &bundled).is_err());
    }

    #[test]
    fn the_bundled_engine_is_found_in_the_app_or_beside_a_dev_build() {
        let app = tempfile::tempdir().unwrap();
        let packaged = app.path().join("Lightbulb.app/Contents/Resources/bin/lightbulb");
        fake(&packaged, "0.6.0");
        assert_eq!(bundled(&app.path().join("Lightbulb.app/Contents/MacOS/lightbulb-desktop")), Some(packaged));
        assert_eq!(bundled(&app.path().join("elsewhere/lightbulb-desktop")), None);
    }
}
