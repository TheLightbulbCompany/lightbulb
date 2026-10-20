//! `lightbulb-desktop serve`: local sessions with no window. The same controller the window
//! runs (`local_sessions::enrollment`), started from the persisted pair state and enrollment
//! instead of a Clerk sign-in. One of `serve` and the window runs it: whoever holds
//! `serve.lock` first; the other only views.
use crate::local_sessions::enrollment::{local_state_dir, Controller};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const LABEL: &str = "company.thelightbulb.serve";

/// `~/Library/Application Support/company.thelightbulb.desktop`: what Tauri resolves for the
/// app's identifier.
pub fn app_data_dir() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
        .join("Library/Application Support/company.thelightbulb.desktop")
}

/// An exclusive `flock` on `<data>/serve.lock`, held while the file lives. Ok(None) when
/// another process holds it; Err when the dir or file is unusable.
pub(crate) fn service_lock(data: &Path) -> io::Result<Option<fs::File>> {
    fs::create_dir_all(data)?;
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(data.join("serve.lock"))?;
    match file.try_lock() {
        Ok(()) => Ok(Some(file)),
        Err(fs::TryLockError::WouldBlock) => Ok(None),
        Err(fs::TryLockError::Error(e)) => Err(e),
    }
}

pub fn serve() -> ! {
    std::process::exit(tauri::async_runtime::block_on(run(&app_data_dir())))
}

async fn run(data: &Path) -> i32 {
    use tokio::signal::unix::{signal, SignalKind};
    let controller = Controller::headless();
    match controller.serves(data) {
        Ok(true) => {}
        Ok(false) => {
            println!("already running");
            return 0;
        }
        Err(e) => {
            eprintln!("lightbulb-desktop serve: {e}");
            return 1;
        }
    }
    let dir = match local_state_dir(data) {
        Ok(dir) => dir,
        Err(e) => {
            eprintln!("lightbulb-desktop serve: {e}");
            return 1;
        }
    };
    crate::local_sessions::resume::install_dir(dir.clone());
    let (Ok(mut term), Ok(mut int)) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    ) else {
        return 1;
    };
    crate::log("[serve] local sessions service started");
    let mut watch = Watch::default();
    loop {
        watch.poll(&controller, data, &dir).await;
        tokio::select! {
            _ = term.recv() => break,
            _ = int.recv() => break,
            _ = tokio::time::sleep(STEP) => {}
        }
    }
    controller.stop();
    crate::log("[serve] stopped");
    0
}

const STEP: Duration = Duration::from_secs(2);

/// Turning sharing on or off in the window (a viewer while this runs) rewrites
/// enrollment.json, and a re-pair rewrites pair_state.json; any change restarts sharing. A
/// failed start (an unreadable key, no pair yet) retries with backoff.
/// ponytail: a 2 s look at two local files; a kqueue watch if it ever matters.
#[derive(Default)]
struct Watch {
    seen: (Option<Vec<u8>>, Option<Vec<u8>>),
    retry: Option<(Instant, Duration)>,
}

impl Watch {
    async fn poll(&mut self, controller: &Controller, data: &Path, dir: &Path) {
        let now = (
            fs::read(dir.join("enrollment.json")).ok(),
            fs::read(data.join("pair_state.json")).ok(),
        );
        let due = self.retry.is_some_and(|(at, _)| Instant::now() >= at);
        if now.0.is_none() || (now == self.seen && !due) {
            return;
        }
        // Snapshot BEFORE the start: a write that lands while it runs is a change next poll.
        self.seen = now;
        match controller.serve_enrolled(data).await {
            Ok(()) => self.retry = None,
            Err(e) => {
                crate::log(&format!("[serve] local sharing unavailable: {e}"));
                let wait = self.retry.map_or(STEP, |(_, wait)| next_backoff(wait));
                self.retry = Some((Instant::now() + wait, wait));
            }
        }
    }
}

fn next_backoff(wait: Duration) -> Duration {
    (wait * 2).min(Duration::from_secs(60))
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// launchd starts agents with a minimal PATH; `path` and `home` are the installing shell's, so
/// `serve` finds `claude` and `codex` (and `codex queue` delivery works).
fn launch_agent_plist(exe: &Path, path: &str, home: &str) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
<plist version=\"1.0\">\n<dict>\n\
\t<key>Label</key>\n\t<string>{LABEL}</string>\n\
\t<key>ProgramArguments</key>\n\t<array>\n\t\t<string>{}</string>\n\t\t<string>serve</string>\n\t</array>\n\
\t<key>RunAtLoad</key>\n\t<true/>\n\
\t<key>KeepAlive</key>\n\t<true/>\n\
\t<key>ThrottleInterval</key>\n\t<integer>30</integer>\n\
\t<key>EnvironmentVariables</key>\n\t<dict>\n\
\t\t<key>PATH</key>\n\t\t<string>{}</string>\n\
\t\t<key>HOME</key>\n\t\t<string>{}</string>\n\
\t</dict>\n\
</dict>\n</plist>\n",
        xml_escape(&exe.to_string_lossy()),
        xml_escape(path),
        xml_escape(home),
    )
}

/// `~/Library/LaunchAgents/company.thelightbulb.serve.plist`. While it exists, `serve` owns
/// local sessions and the window only views them.
pub fn launch_agent_path() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
        .join("Library/LaunchAgents")
        .join(format!("{LABEL}.plist"))
}

/// Writes the launch agent plist and (re)loads it.
pub fn install_launch_agent(exe: &Path) -> io::Result<()> {
    let path = launch_agent_path();
    fs::create_dir_all(path.parent().unwrap())?;
    let env = |key| std::env::var(key).unwrap_or_default();
    fs::write(&path, launch_agent_plist(exe, &env("PATH"), &env("HOME")))?;
    let target = format!("gui/{}", unsafe { libc::getuid() });
    // A loaded agent refuses a second bootstrap; unload it first (absent is fine).
    let _ = std::process::Command::new("/bin/launchctl")
        .arg("bootout")
        .arg(&target)
        .arg(&path)
        .status();
    let status = std::process::Command::new("/bin/launchctl")
        .arg("bootstrap")
        .arg(&target)
        .arg(&path)
        .status()?;
    if !status.success() {
        return Err(io::Error::other(format!(
            "launchctl bootstrap {target} failed: {status}"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn second_serve_exits_when_lock_held() {
        let dir = tempfile::tempdir().unwrap();
        let first = service_lock(dir.path())
            .unwrap()
            .expect("first serve takes the lock");
        assert!(
            service_lock(dir.path()).unwrap().is_none(),
            "a second serve must see the lock held"
        );
        // `serve` itself: exits 0 before starting anything; the window's controller views.
        assert_eq!(tauri::async_runtime::block_on(run(dir.path())), 0);
        assert!(!Controller::default().serves(dir.path()).unwrap());
        drop(first);
        assert!(
            service_lock(dir.path()).unwrap().is_some(),
            "the lock frees with its holder"
        );
    }

    fn enrolled(data: &Path) -> PathBuf {
        let dir = local_state_dir(data).unwrap();
        let path = dir.join("enrollment.json");
        fs::write(&path, r#"{"ownerId":"org_1","humanUserId":"user_1","deviceId":"dev","gatewayUrl":"wss://x","enabled":true,"paused":false}"#).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        dir
    }

    static RACE_FILE: std::sync::Mutex<Option<PathBuf>> = std::sync::Mutex::new(None);
    static RACE_CALLS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    /// Stands in for the key read inside `serve_enrolled`: the window turns sharing off meanwhile.
    fn window_writes_during_start() -> crate::local_sessions::Result<crate::keychain::DeviceIdentity>
    {
        if RACE_CALLS.fetch_add(1, std::sync::atomic::Ordering::SeqCst) == 0 {
            let path = RACE_FILE.lock().unwrap().clone().unwrap();
            fs::write(path, r#"{"ownerId":"org_1","humanUserId":"user_1","deviceId":"dev","gatewayUrl":"wss://x","enabled":false,"paused":false}"#).unwrap();
        }
        Err("no key in test".into())
    }

    #[test]
    fn enrollment_write_during_a_start_is_picked_up_next_poll() {
        let data = tempfile::tempdir().unwrap();
        let dir = enrolled(data.path());
        *RACE_FILE.lock().unwrap() = Some(dir.join("enrollment.json"));
        let controller = Controller::with_identity(window_writes_during_start);
        let mut watch = Watch::default();
        tauri::async_runtime::block_on(watch.poll(&controller, data.path(), &dir));
        assert_eq!(RACE_CALLS.load(std::sync::atomic::Ordering::SeqCst), 1);
        // Sharing is now off on disk: the next poll restarts on it instead of absorbing it.
        tauri::async_runtime::block_on(watch.poll(&controller, data.path(), &dir));
        assert!(!crate::local_sessions::enrollment::sharing_on(&dir));
        assert_eq!(
            watch.retry, None,
            "the off enrollment was served (stop), not retried"
        );
    }

    static KEY_READS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    fn unreadable_key() -> crate::local_sessions::Result<crate::keychain::DeviceIdentity> {
        KEY_READS.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        Err("keychain read: denied".into())
    }

    #[test]
    fn unreadable_key_fails_closed_and_retries() {
        let data = tempfile::tempdir().unwrap();
        let dir = enrolled(data.path());
        let controller = Controller::with_identity(unreadable_key);
        let mut watch = Watch::default();
        let reads = || KEY_READS.load(std::sync::atomic::Ordering::SeqCst);
        tauri::async_runtime::block_on(watch.poll(&controller, data.path(), &dir));
        assert_eq!(reads(), 1);
        assert_eq!(watch.retry.map(|(_, wait)| wait), Some(STEP));
        tauri::async_runtime::block_on(watch.poll(&controller, data.path(), &dir));
        assert_eq!(reads(), 1, "no retry before the backoff is due");
        watch.retry = watch.retry.map(|(_, wait)| (Instant::now(), wait));
        tauri::async_runtime::block_on(watch.poll(&controller, data.path(), &dir));
        assert_eq!(reads(), 2);
        assert_eq!(
            watch.retry.map(|(_, wait)| wait),
            Some(Duration::from_secs(4))
        );
        // Nothing was written in place of the missing key: no pair, no run.
        assert!(!data.path().join("pair_state.json").exists());
    }

    #[test]
    fn failed_start_backoff_doubles_to_a_minute() {
        assert_eq!(next_backoff(STEP), Duration::from_secs(4));
        assert_eq!(
            next_backoff(Duration::from_secs(40)),
            Duration::from_secs(60)
        );
        assert_eq!(
            next_backoff(Duration::from_secs(60)),
            Duration::from_secs(60)
        );
    }

    #[test]
    fn plist_runs_serve_with_keepalive() {
        let xml = launch_agent_plist(
            std::path::Path::new("/Applications/Light & bulb.app/Contents/MacOS/lightbulb-desktop"),
            "/Users/a/.local/bin:/opt/homebrew/bin:/usr/bin:/bin",
            "/Users/a",
        );
        assert!(xml.contains(
            "<key>EnvironmentVariables</key>\n\t<dict>\n\t\t<key>PATH</key>\n\t\t<string>/Users/a/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>\n\t\t<key>HOME</key>\n\t\t<string>/Users/a</string>\n\t</dict>"
        ), "{xml}");
        assert!(
            xml.contains("<key>Label</key>\n\t<string>company.thelightbulb.serve</string>"),
            "{xml}"
        );
        assert!(xml.contains(
            "<key>ProgramArguments</key>\n\t<array>\n\t\t<string>/Applications/Light &amp; bulb.app/Contents/MacOS/lightbulb-desktop</string>\n\t\t<string>serve</string>\n\t</array>"
        ), "{xml}");
        assert!(xml.contains("<key>RunAtLoad</key>\n\t<true/>"), "{xml}");
        assert!(xml.contains("<key>KeepAlive</key>\n\t<true/>"), "{xml}");
        assert!(
            xml.contains("<key>ThrottleInterval</key>\n\t<integer>30</integer>"),
            "{xml}"
        );
        assert!(xml.starts_with("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"));
    }

    #[test]
    fn state_dir_helpers_read_enrollment_without_tauri() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        std::fs::write(
            &path,
            r#"{"ownerId":"org_1","humanUserId":"user_1","deviceId":"dev","gatewayUrl":"wss://x","enabled":true,"paused":false}"#,
        )
        .unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        use crate::local_sessions::enrollment::{enrolled_owner, sharing_on};
        assert!(sharing_on(dir.path()));
        assert_eq!(enrolled_owner(dir.path()).as_deref(), Some("org_1"));
        let empty = tempfile::tempdir().unwrap();
        assert!(!sharing_on(empty.path()));
        assert_eq!(enrolled_owner(empty.path()), None);
    }
}
