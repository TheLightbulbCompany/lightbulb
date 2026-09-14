//! Explicit consent and authentication lifecycle, independent of the node host.
use super::*;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex,
};
use tauri::Manager;

fn retained_pair(
    pair: Option<crate::pair_state::PairState>,
    public_key: &str,
) -> Result<crate::pair_state::PairState> {
    let pair = pair.ok_or("Connect this Mac first")?;
    if !crate::node_client::derive_device_id(public_key).is_ok_and(|id| id == pair.device_id_mac) {
        return Err("Connect this Mac first".into());
    }
    Ok(pair)
}

// Enrollment ordering is kept separate from the OS/keychain boundary so disk,
// hook and transport failures exercise the same path as the command.
async fn complete_enable<T>(
    path: &Path,
    pending: Enrollment,
    preflight: impl FnOnce() -> Result<()>,
    remote: impl std::future::Future<Output = Result<Value>>,
    prepare: impl FnOnce() -> Result<T>,
    finish: impl FnOnce(T, Enrollment) -> Result<()>,
) -> Result<()> {
    preflight()?;
    private_write(path, &pending)?;
    let response = remote.await?;
    validate_response(&pending, &response, true)?;
    let prepared = prepare()?;
    let mut active = pending;
    active.enabled = true;
    active.paused = false;
    active.attempt = None;
    finish(prepared, active)
}

fn validate_response(enrollment: &Enrollment, response: &Value, enabled: bool) -> Result<()> {
    if response["ownerId"] != enrollment.owner_id
        || response["humanUserId"] != enrollment.human_user_id
        || response["deviceId"] != enrollment.device_id
        || response["enabled"] != enabled
        || response["revision"].as_str() != enrollment.server_revision.as_deref()
    {
        return Err(
            "Enrollment response mismatch; remote history revocation remains unconfirmed".into(),
        );
    }
    Ok(())
}

fn commit_consent(
    enrollment: Enrollment,
    save: impl FnOnce(&Enrollment) -> Result<()>,
    start: impl FnOnce(Enrollment),
) -> Result<()> {
    save(&enrollment)?;
    start(enrollment);
    Ok(())
}

fn stop_enrollment(
    path: &Path,
    previous: &mut Option<Enrollment>,
    cleanup: impl FnOnce() -> Result<()>,
) -> Result<bool> {
    if let Some(enrollment) = previous {
        enrollment.enabled = false;
        private_write(path, enrollment)?;
    }
    Ok(cleanup().is_ok())
}

struct Running {
    stop: Arc<AtomicBool>,
    task: tauri::async_runtime::JoinHandle<()>,
}

struct PreparedRun {
    stop: Arc<AtomicBool>,
    host: transport::Host,
    identity: crate::keychain::DeviceIdentity,
    socket: tokio::net::UnixDatagram,
}

#[derive(Default)]
pub struct Controller {
    mutation: tokio::sync::Mutex<()>,
    running: Mutex<Option<Running>>,
    human: Mutex<Option<Option<String>>>,
    revision: AtomicU64,
    status: Arc<Mutex<String>>,
}

impl Controller {
    fn stop(&self) {
        if let Ok(mut running) = self.running.lock() {
            if let Some(run) = running.take() {
                run.stop.store(true, Ordering::SeqCst);
                run.task.abort();
            }
        }
        if let Ok(mut status) = self.status.lock() {
            *status = "paused".into();
        }
    }

    fn prepare(&self, app: &tauri::AppHandle, enrollment: &Enrollment) -> Result<PreparedRun> {
        let dir = state_dir(app)?;
        let identity = crate::keychain::load_or_generate(
            crate::keychain::KeychainSlot::Mac,
            crate::KEYCHAIN_SERVICE,
        )?;
        let pair = retained_pair(
            crate::pair_state::load(
                &app.path()
                    .app_data_dir()
                    .map_err(|_| "desktop storage unavailable")?,
            ),
            &identity.public_key_pem,
        )?;
        if pair.owner_id != enrollment.owner_id || pair.device_id_mac != enrollment.device_id {
            return Err("Reconnect the enrolled workspace's Mac first".into());
        }
        let scope = dir.join(fingerprint(
            &json!([
                enrollment.owner_id,
                enrollment.human_user_id,
                enrollment.device_id
            ])
            .to_string(),
        ));
        let stop = Arc::new(AtomicBool::new(false));
        let host = transport::Host::restore(scope, home()?, stop.clone())?;
        let socket = registration_socket()?;
        Ok(PreparedRun {
            stop,
            host,
            identity,
            socket,
        })
    }

    fn publish(
        &self,
        path: Option<&Path>,
        enrollment: Enrollment,
        prepared: PreparedRun,
        revision: u64,
    ) -> Result<()> {
        // Invalidation takes the same human -> running lock order. No auth/set
        // operation can interleave the final durable consent and publication.
        let human = self.human.lock().map_err(|_| "sharing state unavailable")?;
        let mut running = self
            .running
            .lock()
            .map_err(|_| "sharing state unavailable")?;
        require_current(
            &human,
            self.revision.load(Ordering::SeqCst),
            revision,
            &enrollment,
        )?;
        commit_consent(
            enrollment,
            |enrollment| {
                if let Some(path) = path {
                    private_write(path, enrollment)?;
                }
                Ok(())
            },
            |enrollment| {
                let PreparedRun {
                    stop,
                    host,
                    identity,
                    socket,
                } = prepared;
                let wake = Arc::new(tokio::sync::Notify::new());
                let hook_wake = wake.clone();
                let status = self.status.clone();
                let task = tauri::async_runtime::spawn(async move {
                    let hooks = async move {
                        let mut bytes = [0; 1];
                        loop {
                            if socket.recv(&mut bytes).await.is_err() {
                                // A dead hook socket must never end this select and drop
                                // `supervise` -- park here instead of going dark.
                                transport::relay_log("hook socket error; listener stopped");
                                std::future::pending::<()>().await;
                            }
                            hook_wake.notify_one();
                        }
                    };
                    tokio::select! {_ = hooks=>{},_ = host.supervise(enrollment,identity,wake,status)=>{}}
                });
                *running = Some(Running { stop, task });
            },
        )
    }

    pub async fn restore(&self, app: &tauri::AppHandle) -> Result<()> {
        let _guard = self.mutation.lock().await;
        let path = state_dir(app)?.join("enrollment.json");
        if !path.try_exists().map_err(|_| "sharing state unavailable")? {
            return Ok(());
        }
        let mut enrollment: Enrollment = private_read(&path)?;
        enrollment.authenticate(
            self.human
                .lock()
                .map_err(|_| "sharing state unavailable")?
                .as_ref()
                .and_then(|h| h.as_deref()),
        );
        private_write(&path, &enrollment)?;
        self.stop();
        if !enrollment.enabled || enrollment.paused || enrollment.attempt.is_some() {
            return Ok(());
        }
        let revision = self.revision.load(Ordering::SeqCst);
        let prepared = self.prepare(app, &enrollment)?;
        self.publish(None, enrollment, prepared, revision)
    }
}

fn require_current(
    human: &Option<Option<String>>,
    actual: u64,
    expected: u64,
    enrollment: &Enrollment,
) -> Result<()> {
    if actual != expected
        || human.as_ref().and_then(|h| h.as_deref()) != Some(&enrollment.human_user_id)
    {
        return Err("Authentication changed; local sharing remains stopped".into());
    }
    Ok(())
}

fn predecessor(
    previous: Option<&Enrollment>,
    owner: &str,
    human: &str,
    device: &str,
) -> Result<Option<String>> {
    match previous {
        Some(e) if e.remote_disabled && e.server_revision.is_some() => {
            if e.owner_id == owner && e.human_user_id == human && e.device_id == device {
                Ok(e.server_revision.clone())
            } else {
                // A deliberately re-paired scope can only attempt absent-CAS;
                // never carry another scope's revision or overwrite its row.
                Ok(None)
            }
        }
        Some(_) => Err(
            "Confirm remote history revocation in the enrolled workspace before enabling again"
                .into(),
        ),
        None => Ok(None),
    }
}

fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("local profile unavailable".into())
}
fn state_dir(app: &tauri::AppHandle) -> Result<PathBuf> {
    let app_dir = app
        .path()
        .app_data_dir()
        .map_err(|_| "desktop storage unavailable")?;
    fs::create_dir_all(&app_dir).map_err(|_| "desktop storage unavailable")?;
    let dir = app_dir.join("local-sessions");
    private_dir(&dir)?;
    Ok(dir)
}
fn hook_path() -> PathBuf {
    PathBuf::from(format!("/private/tmp/lightbulb-sessions-{}", unsafe {
        libc::geteuid()
    }))
    .join("wake.sock")
}
fn registration_socket() -> Result<tokio::net::UnixDatagram> {
    use std::os::unix::fs::FileTypeExt;
    let path = hook_path();
    private_dir(path.parent().unwrap())?;
    // This directory is private and the Tauri process is single-instance.
    if let Ok(meta) = fs::symlink_metadata(&path) {
        if !meta.file_type().is_socket() || meta.uid() != unsafe { libc::geteuid() } {
            return Err("unsafe registration channel".into());
        }
        fs::remove_file(&path).map_err(|_| "registration channel unavailable")?;
    }
    tokio::net::UnixDatagram::bind(path).map_err(|_| "registration channel unavailable".into())
}

/// Hooks carry only a wake signal, never a session path, credential, or authority.
pub fn hook() {
    let path = hook_path();
    if private_dir(path.parent().unwrap()).is_err() {
        return;
    }
    if let Ok(socket) = std::os::unix::net::UnixDatagram::unbound() {
        let _ = socket.set_write_timeout(Some(std::time::Duration::from_millis(100)));
        let _ = socket.send_to(b"1", path);
    }
}

#[tauri::command]
pub async fn local_sessions_auth(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
    human_user_id: Option<String>,
) -> Result<()> {
    // Invalidate in-flight enrollment before waiting for its network request.
    {
        let mut human = state
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?;
        state.revision.fetch_add(1, Ordering::SeqCst);
        *human = Some(human_user_id);
        state.stop();
    }
    state.restore(&app).await
}

#[tauri::command]
pub async fn local_sessions_status(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
) -> Result<Value> {
    let _guard = state.mutation.lock().await;
    let path = state_dir(&app)?.join("enrollment.json");
    let enrollment: Option<Enrollment> =
        if path.try_exists().map_err(|_| "sharing state unavailable")? {
            Some(private_read(&path)?)
        } else {
            None
        };
    let paired = crate::pair_state::load(
        &app.path()
            .app_data_dir()
            .map_err(|_| "desktop storage unavailable")?,
    );
    Ok(
        json!({"enrollment":enrollment.map(|e|json!({"ownerId":e.owner_id,"humanUserId":e.human_user_id,"enabled":e.enabled,"paused":e.paused,"remoteDisabled":e.remote_disabled,"recoveryPending":!e.enabled && !e.remote_disabled})),
        "pairedWorkspaceId":paired.map(|p|p.owner_id),"status":state.status.lock().map_err(|_|"sharing state unavailable")?.clone()}),
    )
}

#[tauri::command]
pub async fn local_sessions_set(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
    enabled: bool,
    token: Option<String>,
    human_user_id: Option<String>,
    workspace_id: Option<String>,
) -> Result<u64> {
    let revision = {
        let _human = state
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?;
        let revision = state.revision.fetch_add(1, Ordering::SeqCst) + 1;
        state.stop();
        revision
    };
    let _guard = state.mutation.lock().await;
    let dir = state_dir(&app)?;
    let path = dir.join("enrollment.json");
    let mut previous: Option<Enrollment> =
        if path.try_exists().map_err(|_| "sharing state unavailable")? {
            Some(private_read(&path)?)
        } else {
            None
        };
    if !enabled {
        // Hook cleanup is advisory after durable local stop. Even an unreadable
        // hook record must not hide the revision needed for remote cancellation.
        let cleaned = stop_enrollment(&path, &mut previous, || -> Result<()> {
            let settings = home()?.join(".claude/settings.json");
            let record = dir.join("hook-executable.json");
            let installed = if record.try_exists().map_err(|_| "hook state unavailable")? {
                private_read(&record)?
            } else {
                std::env::current_exe().map_err(|_| "desktop executable unavailable")?
            };
            if settings
                .try_exists()
                .map_err(|_| "Claude settings unavailable")?
            {
                claude::configure_hooks(&settings, &installed, false)?;
            }
            Ok(())
        })?;
        // Skill removal is tracked separately from hook cleanup so a failure in
        // one is never misattributed to the other in the status message.
        let skill_removed = home().and_then(|h| remove_skill(&dir, &h)).is_ok();
        let message = match (!cleaned, !skill_removed) {
            (true, true) => Some(format!(
                "{} {}",
                "Local sharing stopped; Claude wake-hook removal failed. Retry disable after repairing Claude settings.",
                "Local sharing stopped; the lightbulb-session-sharing skill could not be removed from ~/.claude/skills or ~/.codex/skills. Delete it by hand."
            )),
            (true, false) => Some("Local sharing stopped; Claude wake-hook removal failed. Retry disable after repairing Claude settings.".into()),
            (false, true) => Some("Local sharing stopped; the lightbulb-session-sharing skill could not be removed from ~/.claude/skills or ~/.codex/skills. Delete it by hand.".into()),
            (false, false) => None,
        };
        if let Some(message) = message {
            *state
                .status
                .lock()
                .map_err(|_| "sharing state unavailable")? = message;
        }
        return Ok(revision);
    }
    if let Some(enrollment) = &mut previous {
        enrollment.enabled = false;
        private_write(&path, enrollment)?;
    }
    let settings = home()?.join(".claude/settings.json");
    let executable = std::env::current_exe().map_err(|_| "desktop executable unavailable")?;
    let hook_record = dir.join("hook-executable.json");
    let installed: PathBuf = if hook_record
        .try_exists()
        .map_err(|_| "hook state unavailable")?
    {
        private_read(&hook_record)?
    } else {
        executable.clone()
    };
    let human = human_user_id.ok_or("Sign in before enabling sharing")?;
    if state
        .human
        .lock()
        .map_err(|_| "sharing state unavailable")?
        .as_ref()
        != Some(&Some(human.clone()))
    {
        return Err("Sign in before enabling sharing".into());
    }
    let identity = crate::keychain::load_or_generate(
        crate::keychain::KeychainSlot::Mac,
        crate::KEYCHAIN_SERVICE,
    )?;
    let pair = retained_pair(
        crate::pair_state::load(
            &app.path()
                .app_data_dir()
                .map_err(|_| "desktop storage unavailable")?,
        ),
        &identity.public_key_pem,
    )?;
    if workspace_id.as_deref() != Some(&pair.owner_id) {
        return Err("Connect this Mac in the selected workspace first".into());
    }
    let previous_revision = predecessor(
        previous.as_ref(),
        &pair.owner_id,
        &human,
        &pair.device_id_mac,
    )?;
    let enrollment = Enrollment {
        server_revision: Some(uuid::Uuid::new_v4().to_string()),
        owner_id: pair.owner_id,
        human_user_id: human,
        device_id: pair.device_id_mac,
        gateway_url: pair.gateway_url,
        enabled: false,
        paused: true,
        remote_disabled: false,
        attempt: Some(EnrollmentAttempt { previous_revision }),
    };
    let body = enable_body(&enrollment);
    let token = token.as_deref().ok_or("Sign in before enabling sharing")?;
    let hooks = settings.parent().is_some_and(|p| p.is_dir());
    complete_enable(
        &path,
        enrollment.clone(),
        || {
            if hooks {
                claude::preflight_hooks(&settings, &executable)
            } else {
                Ok(())
            }
        },
        enroll_remote(token, body),
        || {
            require_current(
                &*state
                    .human
                    .lock()
                    .map_err(|_| "sharing state unavailable")?,
                state.revision.load(Ordering::SeqCst),
                revision,
                &enrollment,
            )?;
            if hooks {
                if installed != executable {
                    claude::configure_hooks(&settings, &installed, false)?;
                }
                claude::configure_hooks(&settings, &executable, true)?;
                private_write(&hook_record, &executable)?;
            }
            state.prepare(&app, &enrollment)
        },
        |prepared, active| state.publish(Some(&path), active, prepared, revision),
    )
    .await?;
    if let Err(reason) = home().and_then(|h| install_skill(&dir, &h)) {
        *state.status.lock().map_err(|_| "sharing state unavailable")? =
            format!("Sharing enabled; the lightbulb-session-sharing skill was not installed ({reason}). Claude/Codex still receive labelled teammate messages.");
    }
    Ok(revision)
}

#[tauri::command]
pub async fn local_sessions_revoke(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
    revision: u64,
    token: String,
    human_user_id: String,
    workspace_id: String,
) -> Result<()> {
    // Serialize the entire remote request with enable, not merely its initial
    // revision check. A newer enable cannot race behind an old revoke response.
    let _guard = state.mutation.lock().await;
    if state.revision.load(Ordering::SeqCst) != revision {
        return Ok(());
    }
    let path = state_dir(&app)?.join("enrollment.json");
    let mut enrollment: Enrollment = private_read(&path)?;
    if enrollment.enabled
        || enrollment.human_user_id != human_user_id
        || enrollment.owner_id != workspace_id
        || state
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?
            .as_ref()
            != Some(&Some(human_user_id))
    {
        return Ok(());
    }
    // Old consent may predate server revisions. Never fall back to an
    // unconditional background revoke, including after a timeout/conflict.
    if enrollment.remote_disabled {
        return Ok(());
    }
    if enrollment.server_revision.is_none() {
        return Err("Remote history revocation cannot be confirmed: saved enrollment revision is unavailable".into());
    }
    let response = enroll_remote(&token, disable_body(&enrollment)).await?;
    validate_response(&enrollment, &response, false)?;
    enrollment.remote_disabled = true;
    enrollment.attempt = None;
    private_write(&path, &enrollment)?;
    Ok(())
}

fn enable_body(enrollment: &Enrollment) -> Value {
    json!({"deviceId":enrollment.device_id,"ownerId":enrollment.owner_id,"enabled":true,
        "enableRevision":enrollment.server_revision,"previousRevision":enrollment.attempt.as_ref().and_then(|a|a.previous_revision.as_ref())})
}

fn disable_body(enrollment: &Enrollment) -> Value {
    let mut body = json!({"deviceId":enrollment.device_id,"ownerId":enrollment.owner_id,"enabled":false,"expectedRevision":enrollment.server_revision});
    if let Some(attempt) = &enrollment.attempt {
        body["previousRevision"] = json!(attempt.previous_revision);
    }
    body
}

pub(crate) const SKILL_MD: &str = include_str!("../../skills/lightbulb-session-sharing/SKILL.md");
const SKILL_DIR: &str = "lightbulb-session-sharing";

/// Bundled skill into each installed harness's user skill root. Only directories this desktop creates are recorded.
/// Intermediate ancestors (e.g. a dotfile-managed `~/.claude`) may be symlinks by the user's own design and are followed on purpose; only the leaf skill directory and `SKILL.md` are never followed as symlinks.
pub(crate) fn install_skill(state_dir: &Path, home: &Path) -> Result<Vec<PathBuf>> {
    let record = state_dir.join("skill-paths.json");
    let mut created: Vec<PathBuf> = if record.try_exists().map_err(|_| "skill state unavailable")? {
        private_read(&record)?
    } else {
        vec![]
    };
    for root in [".claude", ".codex"] {
        if !home.join(root).is_dir() {
            continue;
        }
        let dir = home.join(root).join("skills").join(SKILL_DIR);
        let file = dir.join("SKILL.md");
        let ours = created.contains(&dir);
        // A pre-existing directory we did not create (or any symlink) is someone else's; leave it.
        match fs::symlink_metadata(&dir) {
            Ok(meta) if meta.file_type().is_symlink() => continue,
            Ok(_) if !ours => continue,
            _ => {}
        }
        fs::create_dir_all(&dir).map_err(|_| "skill directory unavailable")?;
        // A SKILL.md swapped for a symlink after the first install must never be written through.
        match fs::symlink_metadata(&file) {
            Ok(meta) if !meta.is_file() => continue,
            _ => {}
        }
        if fs::read_to_string(&file).ok().as_deref() != Some(SKILL_MD) {
            // Skill text is not a secret; a plain write with the default mode is what the harness expects.
            fs::write(&file, SKILL_MD).map_err(|_| "skill write failed")?;
        }
        if !ours {
            created.push(dir);
        }
    }
    private_write(&record, &created)?;
    Ok(created)
}

pub(crate) fn remove_skill(state_dir: &Path, _home: &Path) -> Result<()> {
    let record = state_dir.join("skill-paths.json");
    if !record.try_exists().map_err(|_| "skill state unavailable")? {
        return Ok(());
    }
    let created: Vec<PathBuf> = private_read(&record)?;
    for dir in created {
        let file = dir.join("SKILL.md");
        if fs::read_to_string(&file).ok().as_deref() == Some(SKILL_MD) {
            let _ = fs::remove_file(&file);
            let _ = fs::remove_dir(&dir);
        }
    }
    fs::remove_file(&record).map_err(|_| "skill state cleanup failed")?;
    Ok(())
}

async fn enroll_remote(token: &str, body: Value) -> Result<Value> {
    let response = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "enrollment unavailable")?
        .put(format!(
            "{}/api/v1/desktop/local-sessions/enrollment",
            crate::LIGHTBULB_API_BASE
        ))
        .bearer_auth(token)
        .json(&body)
        .send()
        .await
        .map_err(|_| "enrollment unavailable")?;
    if !response.status().is_success() {
        return Err(
            "Enrollment rejected; verify workspace membership and Connect this Mac first".into(),
        );
    }
    response
        .json()
        .await
        .map_err(|_| "invalid enrollment response".into())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn candidate() -> Enrollment {
        Enrollment {
            owner_id: "org".into(),
            human_user_id: "alice".into(),
            device_id: "mac".into(),
            gateway_url: "wss://example.invalid".into(),
            enabled: false,
            paused: true,
            server_revision: Some("aaaaaaaa-1111-4111-8111-111111111111".into()),
            remote_disabled: false,
            attempt: Some(EnrollmentAttempt {
                previous_revision: Some("bbbbbbbb-2222-4222-8222-222222222222".into()),
            }),
        }
    }
    fn accepted() -> Value {
        json!({"ownerId":"org","humanUserId":"alice","deviceId":"mac","enabled":true,"revision":"aaaaaaaa-1111-4111-8111-111111111111"})
    }
    #[tokio::test]
    async fn local_sessions_managed_hook_refusal_precedes_remote_enable() {
        let dir = tempfile::tempdir().unwrap();
        let settings = dir.path().join("settings.json");
        private_write(&settings, &json!({"disableAllHooks":true})).unwrap();
        let calls = std::cell::Cell::new(0);
        let result = complete_enable(
            &dir.path().join("enrollment.json"),
            candidate(),
            || claude::preflight_hooks(&settings, Path::new("/app")),
            async {
                calls.set(calls.get() + 1);
                Ok(accepted())
            },
            || Ok(()),
            |_, _| panic!("must not start"),
        )
        .await;
        assert!(result.is_err());
        assert_eq!(
            calls.get(),
            0,
            "managed refusal must not reauthorize retained history"
        );
    }
    #[tokio::test]
    async fn local_sessions_known_enable_then_hook_failure_retains_recovery_after_restart() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        let settings = dir.path().join("settings.json");
        private_write(&settings, &json!({})).unwrap();
        let result = complete_enable(
            &path,
            candidate(),
            || claude::preflight_hooks(&settings, Path::new("/app")),
            async {
                // Managed settings change while the remote request is in flight.
                private_write(&settings, &json!({"disableAllHooks":true})).unwrap();
                Ok(accepted())
            },
            || claude::configure_hooks(&settings, Path::new("/app"), true),
            |_, _| panic!("must not start"),
        )
        .await;
        assert!(result.is_err());
        let restored: Enrollment =
            private_read(&path).expect("successful enable authority must survive restart");
        assert!(!restored.enabled);
        assert_eq!(
            restored.server_revision.as_deref(),
            Some("aaaaaaaa-1111-4111-8111-111111111111")
        );
        assert_eq!(
            disable_body(&restored),
            json!({"deviceId":"mac","ownerId":"org","enabled":false,"expectedRevision":"aaaaaaaa-1111-4111-8111-111111111111","previousRevision":"bbbbbbbb-2222-4222-8222-222222222222"})
        );
    }
    #[tokio::test]
    async fn local_sessions_pending_write_failure_never_sends_and_unknown_response_keeps_attempt() {
        let dir = tempfile::tempdir().unwrap();
        let calls = std::cell::Cell::new(0);
        assert!(complete_enable(
            dir.path(),
            candidate(),
            || Ok(()),
            async {
                calls.set(1);
                Ok(accepted())
            },
            || Ok(()),
            |_, _| panic!("must not publish")
        )
        .await
        .is_err());
        assert_eq!(calls.get(), 0);
        let path = dir.path().join("enrollment.json");
        assert!(complete_enable(
            &path,
            candidate(),
            || Ok(()),
            async {
                let persisted: Enrollment = private_read(&path).unwrap();
                assert_eq!(
                    persisted,
                    candidate(),
                    "recovery must be durable before dispatch"
                );
                Err("timeout: outcome unknown".into())
            },
            || Ok(()),
            |_, _| panic!("must not publish")
        )
        .await
        .is_err());
        assert_eq!(private_read::<Enrollment>(&path).unwrap(), candidate());
    }
    #[tokio::test]
    async fn local_sessions_wrong_response_scope_never_admits_and_keeps_pinned_recovery() {
        for field in ["ownerId", "humanUserId", "deviceId", "revision", "enabled"] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("enrollment.json");
            let mut response = accepted();
            response[field] = json!("other");
            assert!(complete_enable(
                &path,
                candidate(),
                || Ok(()),
                async { Ok(response) },
                || -> Result<()> { panic!("invalid response must not prepare") },
                |_, _| panic!("must not publish")
            )
            .await
            .is_err());
            assert_eq!(private_read::<Enrollment>(&path).unwrap(), candidate());
        }
    }
    fn prepared(dir: &Path) -> PreparedRun {
        let stop = Arc::new(AtomicBool::new(false));
        PreparedRun {
            host: transport::Host::restore(dir.join("host"), dir.join("profile"), stop.clone())
                .unwrap(),
            stop,
            identity: crate::keychain::generate_keypair(),
            socket: tokio::net::UnixDatagram::unbound().unwrap(),
        }
    }
    #[tokio::test]
    async fn local_sessions_auth_invalidation_at_publication_keeps_disk_stopped() {
        for (human, revision) in [
            (None, 0),
            (Some(None), 0),
            (Some(Some("bob".into())), 0),
            (Some(Some("alice".into())), 1),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("enrollment.json");
            let state = Controller::default();
            *state.human.lock().unwrap() = human;
            state.revision.store(revision, Ordering::SeqCst);
            assert!(complete_enable(
                &path,
                candidate(),
                || Ok(()),
                async { Ok(accepted()) },
                || Ok(prepared(dir.path())),
                |ready, active| state.publish(Some(&path), active, ready, 0)
            )
            .await
            .is_err());
            let mut restored: Enrollment = private_read(&path).unwrap();
            restored.authenticate(Some("alice"));
            assert!(!restored.enabled && restored.attempt.is_some());
            assert!(state.running.lock().unwrap().is_none());
        }
    }
    #[tokio::test]
    async fn local_sessions_final_consent_write_failure_does_not_publish() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        let state = Controller::default();
        *state.human.lock().unwrap() = Some(Some("alice".into()));
        let result = complete_enable(
            &path,
            candidate(),
            || Ok(()),
            async { Ok(accepted()) },
            || {
                let ready = prepared(dir.path());
                fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o500)).unwrap();
                Ok(ready)
            },
            |ready, active| state.publish(Some(&path), active, ready, 0),
        )
        .await;
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        assert!(result.is_err());
        assert_eq!(private_read::<Enrollment>(&path).unwrap(), candidate());
        assert!(state.running.lock().unwrap().is_none());
    }
    #[tokio::test]
    async fn local_sessions_post_rename_error_keeps_disable_authority_without_starting() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        let started = std::cell::Cell::new(false);
        let result = complete_enable(
            &path,
            candidate(),
            || Ok(()),
            async { Ok(accepted()) },
            || Ok(()),
            |_, active| {
                commit_consent(
                    active,
                    |value| {
                        // Model directory-fsync failure AFTER the atomic replacement:
                        // the real private writer has put the active record on disk.
                        private_write(&path, value)?;
                        Err("directory sync failed after rename".into())
                    },
                    |_| started.set(true),
                )
            },
        )
        .await;
        assert!(result.is_err());
        assert!(!started.get());
        let mut disk: Enrollment = private_read(&path).unwrap();
        assert!(disk.enabled && !disk.remote_disabled);
        disk.enabled = false; // the existing local-first Disable transition
        assert_eq!(
            disable_body(&disk)["expectedRevision"],
            "aaaaaaaa-1111-4111-8111-111111111111"
        );
        assert!(disable_body(&disk).get("previousRevision").is_none());
    }
    #[tokio::test]
    async fn local_sessions_hook_record_and_host_preparation_failures_keep_recovery() {
        for hook_record_failure in [true, false] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("enrollment.json");
            let blocked = dir.path().join("blocked");
            if hook_record_failure {
                fs::create_dir(&blocked).unwrap();
            } else {
                private_write(&blocked, &json!({})).unwrap();
            }
            let result = complete_enable(
                &path,
                candidate(),
                || Ok(()),
                async { Ok(accepted()) },
                || {
                    if hook_record_failure {
                        private_write(&blocked, &PathBuf::from("/app"))
                    } else {
                        transport::Host::restore(
                            blocked.clone(),
                            dir.path().join("profile"),
                            Arc::new(AtomicBool::new(false)),
                        )
                        .map(|_| ())
                    }
                },
                |_, _| panic!("must not start"),
            )
            .await;
            assert!(result.is_err());
            assert_eq!(private_read::<Enrollment>(&path).unwrap(), candidate());
        }
    }
    #[test]
    fn local_sessions_absent_predecessor_is_explicit_only_for_nominated_attempts() {
        let mut first = candidate();
        first.attempt.as_mut().unwrap().previous_revision = None;
        assert_eq!(
            enable_body(&first).get("previousRevision"),
            Some(&Value::Null)
        );
        assert_eq!(
            disable_body(&first).get("previousRevision"),
            Some(&Value::Null)
        );
        first.attempt = None;
        assert!(disable_body(&first).get("previousRevision").is_none());
    }
    #[test]
    fn local_sessions_hook_cleanup_failure_still_allows_revision_checked_revoke() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        let mut active = candidate();
        active.enabled = true;
        let mut previous = Some(active);
        let cleaned = stop_enrollment(&path, &mut previous, || {
            assert!(
                !private_read::<Enrollment>(&path).unwrap().enabled,
                "local stop precedes hook cleanup"
            );
            Err("hook record unreadable".into())
        })
        .expect("hook failure must not prevent returning the disable revision");
        assert!(!cleaned);
        let disk: Enrollment = private_read(&path).unwrap();
        assert_eq!(
            disable_body(&disk)["expectedRevision"],
            "aaaaaaaa-1111-4111-8111-111111111111"
        );
    }
    #[test]
    fn local_sessions_explicit_new_scope_requires_old_scope_confirmed_disabled() {
        let mut old = candidate();
        assert!(predecessor(Some(&old), "new-org", "alice", "new-mac").is_err());
        old.remote_disabled = true;
        old.attempt = None;
        assert_eq!(
            predecessor(Some(&old), "new-org", "alice", "new-mac").unwrap(),
            None
        );
        assert_eq!(
            predecessor(Some(&old), "org", "alice", "mac").unwrap(),
            Some("aaaaaaaa-1111-4111-8111-111111111111".into())
        );
        old.server_revision = None;
        assert!(predecessor(Some(&old), "new-org", "alice", "new-mac").is_err());
    }
    #[test]
    fn local_sessions_enrollment_requires_retained_mac_but_not_cli_or_node_binary() {
        let identity = crate::keychain::generate_keypair();
        let pair = crate::pair_state::PairState {
            version: 2,
            owner_id: "org".into(),
            device_id_mac: crate::node_client::derive_device_id(&identity.public_key_pem).unwrap(),
            device_id_cli: "unavailable-cli".into(),
            gateway_url: "wss://ws-dev.flickmeon.ai".into(),
            relay_url: "wss://bv-dev.flickmeon.ai/api/v1/gateway/ws".into(),
            gateway_token: "unused".into(),
            display_name: "Mac".into(),
        };
        assert_eq!(
            retained_pair(None, &identity.public_key_pem).unwrap_err(),
            "Connect this Mac first"
        );
        assert!(retained_pair(Some(pair.clone()), &identity.public_key_pem).is_ok());
        assert!(retained_pair(
            Some(pair),
            &crate::keychain::generate_keypair().public_key_pem
        )
        .is_err());
    }
    #[test]
    fn local_sessions_skill_install_is_recorded_reversible_and_never_clobbers() {
        let dir = tempfile::tempdir().unwrap();
        let (home, state) = (dir.path().join("home"), dir.path().join("state"));
        fs::create_dir_all(home.join(".claude")).unwrap();
        fs::create_dir_all(home.join(".codex/skills/lightbulb-session-sharing")).unwrap();
        fs::write(
            home.join(".codex/skills/lightbulb-session-sharing/SKILL.md"),
            "user's own file",
        )
        .unwrap();
        private_dir(&state).unwrap();
        let created = install_skill(&state, &home).unwrap();
        assert_eq!(
            created,
            vec![home.join(".claude/skills/lightbulb-session-sharing")]
        );
        assert_eq!(
            fs::read_to_string(home.join(".claude/skills/lightbulb-session-sharing/SKILL.md"))
                .unwrap(),
            SKILL_MD
        );
        assert_eq!(
            fs::read_to_string(home.join(".codex/skills/lightbulb-session-sharing/SKILL.md"))
                .unwrap(),
            "user's own file"
        );
        assert_eq!(install_skill(&state, &home).unwrap(), created, "idempotent");
        remove_skill(&state, &home).unwrap();
        assert!(!home
            .join(".claude/skills/lightbulb-session-sharing")
            .exists());
        assert!(home
            .join(".codex/skills/lightbulb-session-sharing/SKILL.md")
            .exists());
        assert!(!state.join("skill-paths.json").exists());
    }
    #[test]
    fn local_sessions_skill_install_never_writes_through_a_symlinked_skill_file() {
        let dir = tempfile::tempdir().unwrap();
        let (home, state) = (dir.path().join("home"), dir.path().join("state"));
        let claude_dir = home.join(".claude/skills/lightbulb-session-sharing");
        fs::create_dir_all(&claude_dir).unwrap();
        let target = dir.path().join("elsewhere.md");
        fs::write(&target, "someone else's file").unwrap();
        std::os::unix::fs::symlink(&target, claude_dir.join("SKILL.md")).unwrap();
        private_dir(&state).unwrap();
        private_write(&state.join("skill-paths.json"), &vec![claude_dir.clone()]).unwrap();
        install_skill(&state, &home).unwrap();
        assert_eq!(fs::read_to_string(&target).unwrap(), "someone else's file");
        assert!(fs::symlink_metadata(claude_dir.join("SKILL.md"))
            .unwrap()
            .file_type()
            .is_symlink());
    }
}
