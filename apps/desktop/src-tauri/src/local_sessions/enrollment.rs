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

struct Running {
    stop: Arc<AtomicBool>,
    task: tauri::async_runtime::JoinHandle<()>,
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

    fn start(&self, app: &tauri::AppHandle, enrollment: Enrollment) -> Result<()> {
        if !enrollment.enabled || enrollment.paused {
            return Ok(());
        }
        // Publish the handle under the same lock used by stop, so an auth
        // invalidation cannot slip between spawn and handle publication.
        let mut running = self
            .running
            .lock()
            .map_err(|_| "sharing state unavailable")?;
        if self
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?
            .as_ref()
            .is_some_and(|h| h.as_deref() != Some(&enrollment.human_user_id))
        {
            return Ok(());
        }
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
        let wake = Arc::new(tokio::sync::Notify::new());
        let socket = registration_socket()?;
        let hook_wake = wake.clone();
        let status = self.status.clone();
        let task = tauri::async_runtime::spawn(async move {
            let hooks = async move {
                let mut bytes = [0; 1];
                loop {
                    if socket.recv(&mut bytes).await.is_err() {
                        break;
                    }
                    hook_wake.notify_one();
                }
            };
            tokio::select! {_ = hooks=>{},_ = host.supervise(enrollment,identity,wake,status)=>{}}
        });
        *running = Some(Running { stop, task });
        Ok(())
    }

    pub async fn restore(&self, app: &tauri::AppHandle) -> Result<()> {
        let _guard = self.mutation.lock().await;
        let path = state_dir(app)?.join("enrollment.json");
        if !path.try_exists().map_err(|_| "sharing state unavailable")? {
            return Ok(());
        }
        let mut enrollment: Enrollment = private_read(&path)?;
        if let Some(human) = &*self.human.lock().map_err(|_| "sharing state unavailable")? {
            enrollment.authenticate(human.as_deref());
        }
        private_write(&path, &enrollment)?;
        self.stop();
        self.start(app, enrollment)
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
    state.revision.fetch_add(1, Ordering::SeqCst);
    *state
        .human
        .lock()
        .map_err(|_| "sharing state unavailable")? = Some(human_user_id);
    state.stop();
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
        json!({"enrollment":enrollment.map(|e|json!({"ownerId":e.owner_id,"humanUserId":e.human_user_id,"enabled":e.enabled,"paused":e.paused})),
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
) -> Result<()> {
    let revision = state.revision.fetch_add(1, Ordering::SeqCst) + 1;
    state.stop();
    let _guard = state.mutation.lock().await;
    let dir = state_dir(&app)?;
    let path = dir.join("enrollment.json");
    let mut previous: Option<Enrollment> =
        if path.try_exists().map_err(|_| "sharing state unavailable")? {
            Some(private_read(&path)?)
        } else {
            None
        };
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
    if !enabled {
        if settings.try_exists().unwrap_or(false) {
            claude::configure_hooks(&settings, &installed, false)?;
        }
        // Local disable works offline. The remote transport loses authority and liveness;
        // an available human token additionally revokes the enrollment immediately.
        if let (Some(previous), Some(token)) = (previous, token) {
            if workspace_id.as_deref() == Some(&previous.owner_id) {
                enroll_remote(&token, &previous.device_id, false).await?;
            }
        }
        return Ok(());
    }
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
    let response = enroll_remote(
        token.as_deref().ok_or("Sign in before enabling sharing")?,
        &pair.device_id_mac,
        true,
    )
    .await?;
    if response["humanUserId"] != human
        || response["deviceId"] != pair.device_id_mac
        || response["enabled"] != true
    {
        return Err("Enrollment identity mismatch".into());
    }
    if state.revision.load(Ordering::SeqCst) != revision {
        return Err("Authentication changed; sharing remains paused".into());
    }
    // Ordinary Codex queue requires no launch configuration. Claude gets only a wake hook.
    if settings.parent().is_some_and(|p| p.is_dir()) {
        if installed != executable {
            claude::configure_hooks(&settings, &installed, false)?;
        }
        claude::configure_hooks(&settings, &executable, true)?;
        private_write(&hook_record, &executable)?;
    }
    let enrollment = Enrollment {
        owner_id: pair.owner_id,
        human_user_id: human,
        device_id: pair.device_id_mac,
        gateway_url: pair.gateway_url,
        enabled: true,
        paused: false,
    };
    private_write(&path, &enrollment)?;
    state.start(&app, enrollment)
}

async fn enroll_remote(token: &str, device: &str, enabled: bool) -> Result<Value> {
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
        .json(&json!({"deviceId":device,"enabled":enabled}))
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
    #[test]
    fn local_sessions_enrollment_requires_retained_mac_but_not_cli_or_node_binary() {
        let identity = crate::keychain::generate_keypair();
        let pair = crate::pair_state::PairState {
            version: 2,
            owner_id: "org".into(),
            device_id_mac: crate::node_client::derive_device_id(&identity.public_key_pem).unwrap(),
            device_id_cli: "unavailable-cli".into(),
            gateway_url: "wss://ws-dev.flickmeon.ai".into(),
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
}
