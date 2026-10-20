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

/// An exclusive `flock` on `<data>/serve.lock`, held while the file lives. None when another
/// process holds it (or the dir is unusable).
pub(crate) fn service_lock(data: &Path) -> Option<fs::File> {
    fs::create_dir_all(data).ok()?;
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(data.join("serve.lock"))
        .ok()?;
    file.try_lock().ok()?;
    Some(file)
}

pub fn serve() -> ! {
    std::process::exit(tauri::async_runtime::block_on(run(&app_data_dir())))
}

async fn run(data: &Path) -> i32 {
    use tokio::signal::unix::{signal, SignalKind};
    let controller = Controller::default();
    if !controller.serves(data) {
        println!("already running");
        return 0;
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
    // Turning sharing on or off in the window (a viewer while this runs) rewrites
    // enrollment.json, and a re-pair rewrites pair_state.json; restart on any change. Read back
    // after `restore`, which rewrites the enrollment too. A failed start retries with backoff.
    // ponytail: a 2 s look at two local files; a kqueue watch if it ever matters.
    let files = || {
        (
            fs::read(dir.join("enrollment.json")).ok(),
            fs::read(data.join("pair_state.json")).ok(),
        )
    };
    let mut seen = (None, None);
    let mut retry: Option<(Instant, Duration)> = None;
    loop {
        let now = files();
        let due = retry.is_some_and(|(at, _)| Instant::now() >= at);
        if now.0.is_some() && (now != seen || due) {
            match controller.serve_enrolled(data).await {
                Ok(()) => retry = None,
                Err(e) => {
                    crate::log(&format!("[serve] local sharing unavailable: {e}"));
                    let wait = retry.map_or(STEP, |(_, wait)| next_backoff(wait));
                    retry = Some((Instant::now() + wait, wait));
                }
            }
            seen = files();
        }
        tokio::select! {
            _ = term.recv() => break,
            _ = int.recv() => break,
            _ = tokio::time::sleep(STEP) => {}
        }
    }
    crate::log("[serve] stopped");
    0
}

const STEP: Duration = Duration::from_secs(2);

fn next_backoff(wait: Duration) -> Duration {
    (wait * 2).min(Duration::from_secs(60))
}

fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn launch_agent_plist(exe: &Path) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n\
<plist version=\"1.0\">\n<dict>\n\
\t<key>Label</key>\n\t<string>{LABEL}</string>\n\
\t<key>ProgramArguments</key>\n\t<array>\n\t\t<string>{}</string>\n\t\t<string>serve</string>\n\t</array>\n\
\t<key>RunAtLoad</key>\n\t<true/>\n\
\t<key>KeepAlive</key>\n\t<true/>\n\
\t<key>ThrottleInterval</key>\n\t<integer>30</integer>\n\
</dict>\n</plist>\n",
        xml_escape(&exe.to_string_lossy())
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
    fs::write(&path, launch_agent_plist(exe))?;
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
        let first = service_lock(dir.path()).expect("first serve takes the lock");
        assert!(
            service_lock(dir.path()).is_none(),
            "a second serve must see the lock held"
        );
        // `serve` itself: exits 0 before starting anything; the window's controller views.
        assert_eq!(tauri::async_runtime::block_on(run(dir.path())), 0);
        assert!(!Controller::default().serves(dir.path()));
        drop(first);
        assert!(
            service_lock(dir.path()).is_some(),
            "the lock frees with its holder"
        );
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
        let xml = launch_agent_plist(std::path::Path::new(
            "/Applications/Light & bulb.app/Contents/MacOS/lightbulb-desktop",
        ));
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
