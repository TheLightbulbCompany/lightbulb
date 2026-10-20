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

/// Boots out and removes the launch agent. Absent is fine.
pub fn uninstall_launch_agent() -> io::Result<()> {
    let path = launch_agent_path();
    let _ = std::process::Command::new("/bin/launchctl")
        .arg("bootout")
        .arg(format!("gui/{}", unsafe { libc::getuid() }))
        .arg(&path)
        .status();
    match fs::remove_file(&path) {
        Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
        _ => Ok(()),
    }
}

/// This Mac's name for the approver: `scutil --get ComputerName`, else `hostname`; at most 64
/// printable characters (the backend's limit).
fn machine_name() -> String {
    let run = |cmd: &str, args: &[&str]| {
        std::process::Command::new(cmd)
            .args(args)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_owned())
            .filter(|s| !s.is_empty())
    };
    run("/usr/sbin/scutil", &["--get", "ComputerName"])
        .or_else(|| run("/bin/hostname", &[]))
        .unwrap_or_else(|| "Mac".into())
        .chars()
        .filter(|c| !c.is_control())
        .take(64)
        .collect()
}

fn open_browser(url: &str) {
    let _ = std::process::Command::new("/usr/bin/open").arg(url).status();
}

async fn post(client: &reqwest::Client, url: String, body: serde_json::Value) -> Result<serde_json::Value, String> {
    let resp = client.post(url).json(&body).send().await.map_err(|e| e.to_string())?;
    let status = resp.status();
    if !status.is_success() {
        return Err(format!("{status}: {}", resp.text().await.unwrap_or_default()));
    }
    resp.json().await.map_err(|e| e.to_string())
}

/// The browser handoff: start, show the code, poll until approved or `expiresIn`, then write
/// pair_state.json and enrollment.json in the window's shapes. Returns the line to print.
pub(crate) async fn join_at(
    api: &str,
    data: &Path,
    mac: &crate::keychain::DeviceIdentity,
    cli: &crate::keychain::DeviceIdentity,
    machine: &str,
    open: fn(&str),
) -> Result<String, String> {
    use serde_json::json;
    let api = api.trim_end_matches('/');
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|e| e.to_string())?;
    let start = post(&client, format!("{api}/api/v1/auth/device/start"), json!({
        "tauriMacPubKey": mac.public_key_pem, "cliPubKey": cli.public_key_pem, "machineName": machine,
    }))
    .await
    .map_err(|e| format!("Could not start: {e}"))?;
    let (Some(code), Some(user_code), Some(verify_url)) =
        (start["code"].as_str(), start["userCode"].as_str(), start["verifyUrl"].as_str())
    else {
        return Err("Could not start: unexpected reply".into());
    };
    println!("Open {verify_url} and enter {user_code}");
    open(verify_url);
    let interval = Duration::from_secs_f64(start["interval"].as_f64().unwrap_or(5.0).max(0.0));
    let deadline = Instant::now() + Duration::from_secs_f64(start["expiresIn"].as_f64().unwrap_or(600.0).max(0.0));
    let result = loop {
        if Instant::now() >= deadline {
            return Err("The code expired. Run `lightbulb join` again.".into());
        }
        // A failed poll (network, 5xx) is retried until the deadline.
        if let Ok(reply) = post(&client, format!("{api}/api/v1/auth/device/poll"), json!({ "code": code })).await {
            match reply["status"].as_str() {
                Some("approved") => break reply,
                Some("expired") => return Err("The code expired. Run `lightbulb join` again.".into()),
                _ => {}
            }
        }
        tokio::time::sleep(interval).await;
    };
    let pair: crate::pair_client::PairResponse =
        serde_json::from_value(result["pair"].clone()).map_err(|e| format!("Unexpected pair reply: {e}"))?;
    let state = crate::pair_state::PairState::from_pair_response(
        &pair,
        machine,
        // What the approve paired with: every switch off.
        Some(crate::pair_client::DesktopPermissions { screen: false, shell: false, browser: false, files: false }),
    );
    if !state.matches_identities(&mac.public_key_pem, &cli.public_key_pem) {
        return Err("The server paired other keys than this Mac's. Run `lightbulb join` again.".into());
    }
    let e = &result["enrollment"];
    let human = e["humanUserId"].as_str().or(result["humanUserId"].as_str()).unwrap_or_default();
    if e["ownerId"] != pair.owner_id || e["deviceId"] != pair.device_id_mac || e["enabled"] != true || human.is_empty() {
        return Err("Unexpected enrollment reply. Run `lightbulb join` again.".into());
    }
    let enrollment = crate::local_sessions::Enrollment {
        owner_id: pair.owner_id.clone(),
        human_user_id: human.to_owned(),
        device_id: pair.device_id_mac.clone(),
        gateway_url: pair.gateway_url.clone(),
        enabled: true,
        paused: false,
        server_revision: e["revision"].as_str().map(str::to_owned),
        remote_disabled: false,
        attempt: None,
    };
    // Pair first: `serve` starts on an enrollment change and needs the pair already there.
    crate::pair_state::save(data, &state)?;
    let dir = local_state_dir(data)?;
    crate::local_sessions::enrollment::save_enrollment(&dir, &enrollment)?;
    Ok(match (result["name"].as_str(), result["workspace"].as_str()) {
        (Some(name), Some(workspace)) => format!("Connected as {name} in {workspace}."),
        _ => "Connected.".into(),
    })
}

fn home_dir() -> PathBuf {
    PathBuf::from(std::env::var_os("HOME").unwrap_or_default())
}

/// `lightbulb join`: pair this Mac from the browser, then install everything the window's
/// enable does and start the service. The only headless path that creates Keychain keys.
pub fn join() -> i32 {
    let run = || -> Result<(), String> {
        let keys = |slot| crate::keychain::load_or_generate(slot, crate::KEYCHAIN_SERVICE);
        let mac = keys(crate::keychain::KeychainSlot::Mac)?;
        let cli = keys(crate::keychain::KeychainSlot::Cli)?;
        let data = app_data_dir();
        let line = tauri::async_runtime::block_on(join_at(
            crate::LIGHTBULB_API_BASE, &data, &mac, &cli, &machine_name(), open_browser,
        ))?;
        let exe = app_exe()?;
        setup_at(&local_state_dir(&data)?, &exe);
        let home = home_dir();
        crate::shell_integration::set(&home, true)?;
        match crate::cli::link_command(&exe, &home) {
            Ok(Some(path_line)) => println!("Add ~/.local/bin to your PATH: {path_line}"),
            Ok(None) => {}
            Err(e) => println!("Note: {e}"),
        }
        install_launch_agent(&exe).map_err(|e| format!("Could not start the service: {e}"))?;
        println!("{line}");
        Ok(())
    };
    report(run())
}

/// The app binary itself, never the `~/.local/bin/lightbulb` link this may run through: hooks
/// and the launch agent name it, and run through the link it would parse as a `lightbulb` command.
fn app_exe() -> Result<PathBuf, String> {
    std::env::current_exe()
        .and_then(fs::canonicalize)
        .map_err(|e| format!("this program's path: {e}"))
}

fn setup_at(dir: &Path, exe: &Path) {
    for note in crate::local_sessions::enrollment::reassert_exe(dir, exe) {
        println!("Note: {note}");
    }
}

/// `lightbulb setup`: hooks, the MCP entry and the skill, for every installed agent.
pub fn setup() -> i32 {
    let run = || -> Result<(), String> {
        setup_at(&local_state_dir(&app_data_dir())?, &app_exe()?);
        println!("Set up Claude Code and Codex for Lightbulb.");
        Ok(())
    };
    report(run())
}

/// `lightbulb share on|off`: the "Share every new session" shell block.
pub fn share(on: bool) -> i32 {
    let result = crate::shell_integration::set(&home_dir(), on);
    if result.is_ok() {
        println!("Share every new session: {}.", if on { "on" } else { "off" });
    }
    report(result)
}

/// `lightbulb leave`: stop and remove the service and turn `share` off. The Mac stays paired.
pub fn leave() -> i32 {
    let result = uninstall_launch_agent()
        .map_err(|e| format!("Could not remove the service: {e}"))
        .and_then(|()| crate::shell_integration::set(&home_dir(), false));
    if result.is_ok() {
        println!("Stopped the Lightbulb service and turned sharing off. This Mac is still paired: disconnect it in the Lightbulb app to unpair it.");
    }
    report(result)
}

fn report<E: std::fmt::Display>(result: Result<(), E>) -> i32 {
    match result {
        Ok(()) => 0,
        Err(e) => {
            eprintln!("lightbulb: {e}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};
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

    fn no_browser(_: &str) {}

    async fn device_server(expires_in: f64, approved: Option<Value>) -> wiremock::MockServer {
        use wiremock::matchers::{body_partial_json, method, path};
        use wiremock::{Mock, ResponseTemplate};
        let server = wiremock::MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/device/start"))
            .and(body_partial_json(json!({"machineName":"Test Mac"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "code":"c0de","userCode":"ABCD-EFGH","verifyUrl":"https://x/auth/connect-machine?code=ABCD-EFGH",
                "interval":0.05,"expiresIn":expires_in})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/api/v1/auth/device/poll"))
            .and(body_partial_json(json!({"code":"c0de"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"status":"pending"})))
            .up_to_n_times(if approved.is_some() { 2 } else { u64::MAX })
            .with_priority(1)
            .mount(&server)
            .await;
        if let Some(body) = approved {
            Mock::given(method("POST"))
                .and(path("/api/v1/auth/device/poll"))
                .respond_with(ResponseTemplate::new(200).set_body_json(body))
                .with_priority(2)
                .mount(&server)
                .await;
        }
        server
    }

    #[test]
    fn join_writes_state_from_poll_result() {
        tauri::async_runtime::block_on(async {
            let mac = crate::keychain::generate_keypair();
            let cli = crate::keychain::generate_keypair();
            let mac_id = crate::node_client::derive_device_id(&mac.public_key_pem).unwrap();
            let cli_id = crate::node_client::derive_device_id(&cli.public_key_pem).unwrap();
            let server = device_server(10.0, Some(json!({"status":"approved",
                "pair":{"gatewayUrl":"wss://ws","relayUrl":"wss://bv/api/v1/gateway/ws","gatewayToken":"",
                    "ownerId":"org_1","deviceIdMac":mac_id,"deviceIdCli":cli_id,"commandsMac":[],"commandsCli":[]},
                "enrollment":{"deviceId":mac_id,"humanUserId":"user_1","cliDeviceId":cli_id,"ownerId":"org_1",
                    "enabled":true,"revision":"rev-1"},
                "humanUserId":"user_1"}))).await;
            let data = tempfile::tempdir().unwrap();
            let line = join_at(&server.uri(), data.path(), &mac, &cli, "Test Mac", no_browser).await.unwrap();
            assert_eq!(line, "Connected.");
            let pair = crate::pair_state::load(data.path()).expect("pair_state.json in the window's shape");
            assert_eq!((pair.owner_id.as_str(), pair.device_id_mac.as_str()), ("org_1", mac_id.as_str()));
            assert_eq!(pair.relay_url, "wss://bv/api/v1/gateway/ws");
            assert_eq!(pair.display_name, "Test Mac");
            assert!(pair.matches_identities(&mac.public_key_pem, &cli.public_key_pem));
            let dir = local_state_dir(data.path()).unwrap();
            let enrollment: crate::local_sessions::Enrollment =
                serde_json::from_slice(&fs::read(dir.join("enrollment.json")).unwrap()).unwrap();
            assert_eq!(enrollment, crate::local_sessions::Enrollment {
                owner_id: "org_1".into(), human_user_id: "user_1".into(), device_id: mac_id.clone(),
                gateway_url: "wss://ws".into(), enabled: true, paused: false,
                server_revision: Some("rev-1".into()), remote_disabled: false, attempt: None,
            });
            assert!(crate::local_sessions::enrollment::sharing_on(&dir), "serve can read it");
            let mode = fs::metadata(dir.join("enrollment.json")).unwrap().permissions().mode();
            assert_eq!(mode & 0o077, 0);
        });
    }

    #[test]
    fn join_times_out_after_expires_in() {
        tauri::async_runtime::block_on(async {
            let mac = crate::keychain::generate_keypair();
            let cli = crate::keychain::generate_keypair();
            let server = device_server(0.3, None).await;
            let data = tempfile::tempdir().unwrap();
            let started = Instant::now();
            let err = join_at(&server.uri(), data.path(), &mac, &cli, "Test Mac", no_browser).await.unwrap_err();
            assert!(err.contains("expired"), "{err}");
            assert!(started.elapsed() < Duration::from_secs(5));
            assert!(!data.path().join("pair_state.json").exists());
        });
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
