//! The agent's own Chrome profile (`lightbulb`, driver `openclaw`) and its wipe.
//!
//! The backend shares workspace browser sign-ins into this profile
//! (`core/services/desktop_browser_state.py`), so its user-data holds workspace
//! cookies, storage and extensions. When this Mac stops being allowed to browse for
//! the workspace (Browser switched off, unpaired here, or the pairing removed on the
//! server) that directory is deleted so no sign-in outlives the access.
//!
//! The path is the bundled node host's own layout: `<state_dir>/browser/<profile>/user-data`
//! (openclaw `resolveOpenClawUserDataDir`, CONFIG_DIR = `OPENCLAW_STATE_DIR`). The
//! person's everyday Chrome (`chrome` extension profile, or their real Chrome) is
//! never touched: only that one fixed directory is.

use std::path::{Component, Path, PathBuf};

const PROFILE: &str = "lightbulb";

/// `<state_dir>/browser/lightbulb/user-data`.
pub fn user_data_dir(state_dir: &Path) -> PathBuf {
    state_dir.join("browser").join(PROFILE).join("user-data")
}

/// Resolve the directory to delete, or `None` when there is nothing to delete.
/// Refuses (Err) a state dir with `..`, and any symlink on the way from the state
/// dir to `user-data` (inclusive), and anything that does not canonicalize to
/// exactly `<canonical state dir>/browser/lightbulb/user-data`.
pub fn wipe_target(state_dir: &Path) -> Result<Option<PathBuf>, String> {
    if !state_dir.is_absolute() || state_dir.components().any(|c| c == Component::ParentDir) {
        return Err(format!("refusing agent browser wipe: bad state dir {}", state_dir.display()));
    }
    let Ok(root) = state_dir.canonicalize() else {
        return Ok(None); // no state dir, no profile
    };
    let mut path = root.clone();
    for part in ["browser", PROFILE, "user-data"] {
        path.push(part);
        match std::fs::symlink_metadata(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(format!("inspect {}: {e}", path.display())),
            Ok(m) if m.file_type().is_symlink() => {
                return Err(format!("refusing agent browser wipe through symlink {}", path.display()))
            }
            Ok(m) if !m.is_dir() => return Err(format!("{} is not a directory", path.display())),
            Ok(_) => {}
        }
    }
    let resolved = path.canonicalize().map_err(|e| format!("resolve {}: {e}", path.display()))?;
    if resolved != user_data_dir(&root) || !resolved.starts_with(&root) {
        return Err(format!("refusing agent browser wipe outside the app state dir: {}", resolved.display()));
    }
    Ok(Some(resolved))
}

/// Stop the agent Chrome, then delete its user-data. `Ok(false)` when there was none.
/// `remove_dir_all` does not follow symlinks found inside the tree.
pub fn wipe(state_dir: &Path) -> Result<bool, String> {
    let Some(dir) = wipe_target(state_dir)? else {
        return Ok(false);
    };
    stop_chrome_for(&dir);
    std::fs::remove_dir_all(&dir).map_err(|e| format!("delete {}: {e}", dir.display()))?;
    crate::log(&format!("[agent-browser] wiped {}", dir.display()));
    Ok(true)
}

/// Chrome's `SingletonLock` in a user-data dir is a symlink to `<host>-<pid>`. Kill
/// that pid, but only after `ps` confirms it is the Chrome launched on exactly this
/// user-data dir (openclaw passes `--user-data-dir=<dir>`), so a recycled pid is
/// never signalled.
pub(crate) fn stop_chrome_for(user_data: &Path) {
    let Some(pid) = chrome_pid_for(user_data) else {
        return;
    };
    crate::log(&format!("[agent-browser] stopping agent Chrome pid {pid}"));
    for signal in [libc::SIGTERM, libc::SIGKILL] {
        // SAFETY: plain syscalls on a pid we just verified.
        unsafe { libc::kill(pid, signal) };
        for _ in 0..50 {
            if unsafe { libc::kill(pid, 0) } != 0 {
                return;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
    }
}

/// The pid in `SingletonLock`, only when `ps` confirms it is the Chrome launched on
/// exactly this user-data dir.
pub(crate) fn chrome_pid_for(user_data: &Path) -> Option<i32> {
    let pid = std::fs::read_link(user_data.join("SingletonLock"))
        .ok()
        .and_then(|t| singleton_pid(&t.to_string_lossy()))?;
    let out = std::process::Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
        .ok()?;
    String::from_utf8_lossy(&out.stdout)
        .contains(&format!("--user-data-dir={}", user_data.display()))
        .then_some(pid)
}

/// `"host-name-1234"` -> 1234.
pub(crate) fn singleton_pid(target: &str) -> Option<i32> {
    target.rsplit_once('-')?.1.parse().ok().filter(|pid| *pid > 1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn state() -> (tempfile::TempDir, PathBuf) {
        let d = tempfile::tempdir().unwrap();
        let s = d.path().canonicalize().unwrap().join("openclaw-state");
        std::fs::create_dir_all(user_data_dir(&s).join("Default")).unwrap();
        std::fs::write(user_data_dir(&s).join("Default/Cookies"), "c").unwrap();
        (d, s)
    }

    #[test]
    fn wipes_only_the_lightbulb_user_data() {
        let (_d, s) = state();
        let everyday = s.join("browser/chrome/user-data");
        std::fs::create_dir_all(&everyday).unwrap();
        std::fs::write(s.join("openclaw.json"), "{}").unwrap();
        assert!(wipe(&s).unwrap());
        assert!(!user_data_dir(&s).exists());
        assert!(s.join("browser/lightbulb").is_dir(), "only user-data goes");
        assert!(everyday.is_dir(), "the chrome profile is never touched");
        assert!(s.join("openclaw.json").exists());
        assert!(!wipe(&s).unwrap(), "second wipe is a no-op");
    }

    #[test]
    fn missing_state_or_profile_is_nothing_to_wipe() {
        let d = tempfile::tempdir().unwrap();
        assert_eq!(wipe_target(&d.path().join("absent")).unwrap(), None);
        std::fs::create_dir_all(d.path().join("s/browser")).unwrap();
        assert_eq!(wipe_target(&d.path().join("s")).unwrap(), None);
    }

    #[test]
    fn refuses_symlinks_on_the_way_and_parent_dir_components() {
        let (d, s) = state();
        let victim = d.path().join("victim");
        std::fs::create_dir_all(&victim).unwrap();
        std::fs::write(victim.join("keep"), "k").unwrap();

        // user-data itself is a link
        std::fs::remove_dir_all(user_data_dir(&s)).unwrap();
        symlink(&victim, user_data_dir(&s)).unwrap();
        assert!(wipe(&s).is_err());
        std::fs::remove_file(user_data_dir(&s)).unwrap();

        // an intermediate component is a link
        std::fs::remove_dir_all(s.join("browser/lightbulb")).unwrap();
        std::fs::create_dir_all(victim.join("user-data")).unwrap();
        symlink(&victim, s.join("browser/lightbulb")).unwrap();
        assert!(wipe(&s).is_err());
        assert!(victim.join("keep").exists() && victim.join("user-data").exists());

        assert!(wipe_target(&s.join("../openclaw-state")).is_err());
        assert!(wipe_target(Path::new("relative/state")).is_err());
    }

    #[test]
    fn a_link_inside_user_data_is_removed_not_followed() {
        let (d, s) = state();
        let outside = d.path().join("outside");
        std::fs::create_dir_all(&outside).unwrap();
        std::fs::write(outside.join("keep"), "k").unwrap();
        symlink(&outside, user_data_dir(&s).join("Default/escape")).unwrap();
        assert!(wipe(&s).unwrap());
        assert!(outside.join("keep").exists());
    }

    #[test]
    fn singleton_pid_parses_host_dash_pid() {
        assert_eq!(singleton_pid("my-mac.local-4321"), Some(4321));
        assert_eq!(singleton_pid("host-1"), None);
        assert_eq!(singleton_pid("nopid"), None);
    }
}
