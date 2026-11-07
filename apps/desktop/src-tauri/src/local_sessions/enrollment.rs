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

struct AbortOnDrop(tauri::async_runtime::JoinHandle<()>);
impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// The mod and tool sockets get a runtime of their own. On the app's shared runtime they
/// went unanswered whenever blocking work elsewhere held every worker (prod, 2026-10-05:
/// all ten workers inside `read`, both sockets silent, the connection loop's 1 s scan taking
/// 69 s). Dropping this stops the servers, so it lives inside the relay task like the rest.
struct SocketsRuntime(Option<tokio::runtime::Runtime>);
impl Drop for SocketsRuntime {
    fn drop(&mut self) {
        // `Runtime`'s own drop blocks, which panics inside the async task that owns this.
        if let Some(runtime) = self.0.take() {
            runtime.shutdown_background();
        }
    }
}

fn serve_sockets(
    servers: Vec<std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>>,
) -> Option<SocketsRuntime> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_name("lightbulb-sockets")
        .enable_all()
        .build()
        .map_err(|e| crate::log(&format!("[local-sessions] socket runtime unavailable: {e}")))
        .ok()?;
    for server in servers {
        runtime.spawn(server);
    }
    Some(SocketsRuntime(Some(runtime)))
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
    /// The host's own state directory; pick-up snapshot state lives under it.
    scope: PathBuf,
}

#[derive(Default)]
pub struct Controller {
    mutation: tokio::sync::Mutex<()>,
    running: Mutex<Option<Running>>,
    human: Mutex<Option<Option<String>>>,
    revision: AtomicU64,
    status: Arc<Mutex<String>>,
    /// Set once `/desktop/pair` answered this launch; until then every new
    /// sign-in token re-asks it for the relay door (`refresh_relay`).
    relay_checked: AtomicBool,
    /// `serve.lock`, once this process owns the service (`serves`).
    service: Mutex<Option<std::fs::File>>,
    /// How `prepare` reads this Mac's key. None: the window's `load_or_generate`.
    load_identity: Option<fn() -> Result<crate::keychain::DeviceIdentity>>,
}

/// `serve`'s key read: load only, never generate (see `keychain::load`).
fn load_mac_identity() -> Result<crate::keychain::DeviceIdentity> {
    crate::keychain::load(crate::keychain::KeychainSlot::Mac, crate::KEYCHAIN_SERVICE)
}

/// The enrollment captured the relay door (`gatewayUrl`) once, at enable time,
/// and the door moves with the product domain (flickmeon.ai ->
/// thelightbulb.company stranded every enrolled Mac). Re-ask `/desktop/pair`
/// -- the same idempotent re-pair the switches path sends, with the switches
/// last granted -- and fall back to the last pair response on disk, so an
/// offline launch keeps what it had. The flag says `/desktop/pair` answered.
/// `data` is the app data dir; an empty `jwt` (no window, or no sign-in yet) only reads the
/// persisted pair state.
async fn current_gateway_url(data: &Path, jwt: &str, ask: bool) -> Option<(String, bool)> {
    let pair = crate::pair_state::load(data)?;
    // No recorded switches means the switches path re-pairs (and rewrites
    // pair_state) on the next token; never re-pair with an implied grant here.
    if ask && !jwt.is_empty() && pair.paired_permissions.is_some() {
        let mac = crate::keychain::load_or_generate(crate::keychain::KeychainSlot::Mac, crate::KEYCHAIN_SERVICE);
        let cli = crate::keychain::load_or_generate(crate::keychain::KeychainSlot::Cli, crate::KEYCHAIN_SERVICE);
        if let (Ok(mac), Ok(cli)) = (mac, cli) {
            let request = crate::pair_client::PairRequest {
                tauri_mac_pub_key: mac.public_key_pem,
                cli_pub_key: cli.public_key_pem,
                permissions: pair.paired_permissions.clone(),
            };
            let asked = tokio::time::timeout(
                std::time::Duration::from_secs(10),
                crate::pair_client::pair(crate::LIGHTBULB_API_BASE, jwt, &request),
            )
            .await;
            match asked {
                Ok(Ok(resp)) if resp.device_id_mac == pair.device_id_mac && resp.device_id_cli == pair.device_id_cli => {
                    let fresh = crate::pair_state::PairState::from_pair_response(&resp, &pair.display_name, pair.paired_permissions.clone());
                    let _ = crate::pair_state::save(data, &fresh);
                    return Some((fresh.gateway_url, true));
                }
                Ok(Ok(_)) => transport::relay_log("re-pair named other devices; relay URL kept"),
                _ => transport::relay_log("/desktop/pair unreachable; relay URL kept"),
            }
        }
    }
    Some((pair.gateway_url, false))
}

/// Rewrite the persisted enrollment's relay door when it differs. True when it did.
fn adopt_gateway_url(path: &Path, url: &str) -> Result<bool> {
    let mut enrollment: Enrollment = private_read(path)?;
    if enrollment.gateway_url == url {
        return Ok(false);
    }
    enrollment.gateway_url = url.to_owned();
    private_write(path, &enrollment)?;
    Ok(true)
}

impl Controller {
    /// Caller holds `mutation`. True when the enrollment's relay door changed.
    async fn refresh_gateway_url(&self, data: &Path, jwt: &str, path: &Path) -> bool {
        let ask = !self.relay_checked.load(Ordering::SeqCst);
        let Some((url, answered)) = current_gateway_url(data, jwt, ask).await else {
            return false;
        };
        if answered {
            self.relay_checked.store(true, Ordering::SeqCst);
        }
        adopt_gateway_url(path, &url).unwrap_or(false)
    }

    /// From `send_auth_token`: the first token of a launch re-asks `/desktop/pair`
    /// for the relay door (restore can run before any token exists), and a moved
    /// door restarts the relay on it.
    pub async fn refresh_relay(&self, app: &tauri::AppHandle) -> Result<()> {
        if self.relay_checked.load(Ordering::SeqCst) {
            return Ok(());
        }
        let changed = {
            let _guard = self.mutation.lock().await;
            let (data, jwt) = app_auth(app)?;
            let path = local_state_dir(&data)?.join("enrollment.json");
            path.try_exists().unwrap_or(false) && self.refresh_gateway_url(&data, &jwt, &path).await
        };
        if changed {
            self.restore(app).await?;
        }
        Ok(())
    }

    /// The page reported the person sharing already runs for. Nothing changed: the relay
    /// connection stays up.
    fn sharing_as(&self, human: Option<&str>) -> bool {
        let same = self.human.lock().is_ok_and(|known| known.as_ref().is_some_and(|known| known.as_deref() == human));
        same && human.is_some() && self.running.lock().is_ok_and(|running| running.is_some())
    }

    /// `serve`'s controller: reads this Mac's key without ever creating one.
    pub(crate) fn headless() -> Self {
        Self::with_identity(load_mac_identity)
    }

    pub(crate) fn with_identity(load: fn() -> Result<crate::keychain::DeviceIdentity>) -> Self {
        Self { load_identity: Some(load), ..Self::default() }
    }

    pub(crate) fn stop(&self) {
        if let Ok(mut running) = self.running.lock() {
            if let Some(run) = running.take() {
                run.stop.store(true, Ordering::SeqCst);
                run.task.abort();
                // The aborted connection logs nothing itself; a reconnect must have a cause on file.
                transport::relay_log("connection closed by the app: sharing stops or restarts");
            }
        }
        if let Ok(mut status) = self.status.lock() {
            *status = "paused".into();
        }
    }

    /// This process runs the local-sessions service: it holds `serve.lock` under the app
    /// data dir, taken on first use and kept for the process's life. False when another
    /// process (a `serve`, or the window app) already runs it. Err: the lock file itself is
    /// unusable (storage), which is not a reason to become a viewer.
    pub(crate) fn serves(&self, data: &Path) -> Result<bool> {
        let mut held = self.service.lock().map_err(|_| "sharing state unavailable")?;
        if held.is_none() {
            *held = crate::headless::service_lock(data).map_err(|e| format!("serve.lock unavailable: {e}"))?;
        }
        Ok(held.is_some())
    }

    /// The window runs the service only when no launch agent is installed (today's single
    /// process). With `agent` on disk, `serve` owns local sessions and the window views from
    /// the start; a lock the window took before the agent was installed is let go.
    pub(crate) fn window_serves(&self, data: &Path, agent: &Path) -> Result<bool> {
        if agent.exists() {
            // Stop any run of ours before `serve` can take the lock and start its own.
            self.stop();
            if let Ok(mut held) = self.service.lock() {
                *held = None;
            }
            return Ok(false);
        }
        self.serves(data)
    }

    /// `lightbulb pair` installed the launch agent while the window runs the service: stop our
    /// run first, then let go of `serve.lock` so `serve` takes it. True when it handed over.
    pub(crate) async fn hand_over_to_agent(&self, data: &Path, agent: &Path) -> bool {
        if !agent.exists() || !self.service.lock().is_ok_and(|held| held.is_some()) {
            return false;
        }
        let _guard = self.mutation.lock().await;
        let _ = self.window_serves(data, agent); // with the agent on disk: stop, then release
        if let Ok(mut status) = self.status.lock() {
            *status = "Local sharing runs in lightbulb-desktop serve.".into();
        }
        true
    }

    /// The window's 2 s look for the launch agent (`hand_over_to_agent`), for the app's life.
    /// ponytail: a 2 s stat of one file, as `serve` watches its own; a kqueue watch if it matters.
    pub fn watch_launch_agent(app: tauri::AppHandle) {
        tauri::async_runtime::spawn(async move {
            let Ok((data, _)) = app_auth(&app) else { return };
            let agent = crate::headless::launch_agent_path();
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(2)).await;
                if app.state::<Controller>().hand_over_to_agent(&data, &agent).await {
                    crate::log("[local-sessions] launch agent installed; the window now only views");
                }
            }
        });
    }

    /// The end of an enable. A viewer only records the consent: `serve` sees enrollment.json
    /// change and starts the run itself.
    fn finish_enable(&self, path: &Path, enrollment: Enrollment, prepared: Option<PreparedRun>, revision: u64) -> Result<()> {
        let Some(prepared) = prepared else {
            let human = self.human.lock().map_err(|_| "sharing state unavailable")?;
            require_current(&human, self.revision.load(Ordering::SeqCst), revision, &enrollment)?;
            return private_write(path, &enrollment);
        };
        self.publish(Some(path), enrollment, prepared, revision)
    }

    fn prepare(&self, data: &Path, enrollment: &Enrollment) -> Result<PreparedRun> {
        let dir = local_state_dir(data)?;
        let identity = match self.load_identity {
            Some(load) => load()?,
            None => crate::keychain::load_or_generate(crate::keychain::KeychainSlot::Mac, crate::KEYCHAIN_SERVICE)?,
        };
        let pair = retained_pair(crate::pair_state::load(data), &identity.public_key_pem)?;
        if pair.owner_id != enrollment.owner_id || pair.device_id_mac != enrollment.device_id {
            return Err("Reconnect the enrolled workspace's Mac first".into());
        }
        let scope = dir.join(scope_key(enrollment));
        let stop = Arc::new(AtomicBool::new(false));
        let host = transport::Host::restore(scope.clone(), home()?, stop.clone())?;
        let socket = registration_socket()?;
        Ok(PreparedRun {
            stop,
            host,
            identity,
            socket,
            scope,
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
                    scope,
                } = prepared;
                let mod_socket = super::mod_socket::serve(scope.clone(), enrollment.clone(), identity.clone());
                // Pick-up snapshots run on their OWN task: sharing one `select!` with the
                // relay would stall every relay frame while a snapshot does its work.
                let snapshots = super::snapshot::run(
                    super::snapshot::channel(),
                    super::snapshot::Author {
                        dir: scope,
                        api: crate::LIGHTBULB_API_BASE.into(),
                        key_pem: identity.private_key_pem.clone(),
                        device: enrollment.device_id.clone(),
                        owner: enrollment.owner_id.clone(),
                    },
                );
                // Resume runs on its own task too: starting a holder must never stall a relay frame.
                let resumes = super::resume::run(
                    super::resume::channel(),
                    super::resume::Resumer {
                        api: crate::LIGHTBULB_API_BASE.into(),
                        key_pem: identity.private_key_pem.clone(),
                        device: enrollment.device_id.clone(),
                        owner: enrollment.owner_id.clone(),
                        human: enrollment.human_user_id.clone(),
                    },
                );
                let wake = Arc::new(tokio::sync::Notify::new());
                let hook_wake = wake.clone();
                let status = self.status.clone();
                let (hooks_tx, hooks_rx) =
                    tokio::sync::mpsc::channel::<super::lifecycle::HookEvent>(64);
                let task = tauri::async_runtime::spawn(async move {
                    // Started from inside the relay task and aborted when it is dropped, so
                    // stopping or restarting sharing stops it; its ending (or panicking)
                    // ends only itself.
                    let _snapshots = AbortOnDrop(tauri::async_runtime::spawn(snapshots));
                    let _resumes = AbortOnDrop(tauri::async_runtime::spawn(resumes));
                    let _sockets = serve_sockets(vec![Box::pin(mod_socket)]);
                    let hooks = async move {
                        let mut bytes = [0u8; 2048];
                        loop {
                            let n = match socket.recv(&mut bytes).await {
                                Ok(n) => n,
                                Err(_) => {
                                    // A dead hook socket must never end this select and drop
                                    // `supervise` -- park here instead of going dark.
                                    transport::relay_log("hook socket error; listener stopped");
                                    std::future::pending::<()>().await;
                                    unreachable!()
                                }
                            };
                            match super::lifecycle::parse_datagram(&bytes[..n]) {
                                Some(event) => {
                                    if super::lifecycle::transition(&event)
                                        == super::lifecycle::Transition::Start
                                    {
                                        hook_wake.notify_one();
                                    }
                                    if hooks_tx.try_send(event).is_err() {
                                        // A lost status/end event is only recovered by
                                        // the 60s sweep, so it must not be silent.
                                        transport::relay_log("hook event dropped: channel full");
                                    }
                                }
                                None => hook_wake.notify_one(),
                            }
                        }
                    };
                    tokio::select! {_ = hooks=>{},_ = host.supervise(enrollment,identity,wake,status,hooks_rx)=>{}}
                });
                *running = Some(Running { stop, task });
            },
        )
    }

    pub async fn restore(&self, app: &tauri::AppHandle) -> Result<()> {
        let (data, jwt) = app_auth(app)?;
        if !self.window_serves(&data, &crate::headless::launch_agent_path())? {
            let _guard = self.mutation.lock().await;
            self.stop();
            if let Ok(mut status) = self.status.lock() {
                *status = "Local sharing runs in lightbulb-desktop serve.".into();
            }
            return Ok(());
        }
        self.restore_at(&data, &jwt).await
    }

    /// `serve`: share as the enrolled human, from persisted state, with no live Clerk check
    /// (nobody signs in with no window). The consent on disk is that human's own and the relay
    /// authenticates with this Mac's key. Unpairing the Mac, or turning sharing off from any
    /// device, still revokes it on the server.
    pub(crate) async fn serve_enrolled(&self, data: &Path) -> Result<()> {
        let enrollment: Enrollment = private_read(&local_state_dir(data)?.join("enrollment.json"))?;
        *self.human.lock().map_err(|_| "sharing state unavailable")? = Some(Some(enrollment.human_user_id));
        self.restore_at(data, "").await
    }

    /// `data` is the app data dir. `jwt` is the window's Clerk token, empty with no window.
    async fn restore_at(&self, data: &Path, jwt: &str) -> Result<()> {
        let _guard = self.mutation.lock().await;
        let dir = local_state_dir(data)?;
        let path = dir.join("enrollment.json");
        if !path.try_exists().map_err(|_| "sharing state unavailable")? {
            return Ok(());
        }
        self.refresh_gateway_url(data, jwt, &path).await;
        let mut enrollment: Enrollment = private_read(&path)?;
        let paused = enrollment.paused;
        enrollment.authenticate(
            self.human
                .lock()
                .map_err(|_| "sharing state unavailable")?
                .as_ref()
                .map(|h| h.as_deref()),
        );
        // Only a change is written: a blind read-then-write here could overwrite a write the
        // window made in between (sharing on or off while `serve` runs).
        if enrollment.paused != paused {
            private_write(&path, &enrollment)?;
        }
        self.stop();
        if !enrollment.enabled || enrollment.paused || enrollment.attempt.is_some() {
            return Ok(());
        }
        let revision = self.revision.load(Ordering::SeqCst);
        let prepared = self.prepare(data, &enrollment)?;
        let notes = reassert(&dir);
        if !notes.is_empty() {
            *self.status.lock().map_err(|_| "sharing state unavailable")? =
                format!("Local sharing resumed; {}.", notes.join("; "));
        }
        self.resume(enrollment, prepared, revision)
    }

    /// The tail of `restore`. At launch nobody has reported who is signed in
    /// yet, and `publish` demands a matching human -- so an unreported human is
    /// a quiet no-op here, not a failure. Sharing comes up on the
    /// `local_sessions_auth` call that follows.
    fn resume(&self, enrollment: Enrollment, prepared: PreparedRun, revision: u64) -> Result<()> {
        if self
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?
            .is_none()
        {
            return Ok(());
        }
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
/// Session sharing is on and live for this Mac's signed-in human. A remotely started
/// session is only launched when it will be mirrored (see `session_start`).
/// `state_dir` is the local-sessions dir (`state_dir(app)` in the window).
pub(crate) fn sharing_on(state_dir: &Path) -> bool {
    private_read::<Enrollment>(&state_dir.join("enrollment.json"))
        .is_ok_and(|e| e.enabled && !e.paused && !e.remote_disabled)
}
/// The sidebar's Share on an engine row. The page names a row; the session, and the words the
/// person is asked about, come from the engine's own live rows, so a page cannot name a session
/// that is not this Mac's own, nor show one row and share another.
#[tauri::command]
pub async fn share_engine_session(app: tauri::AppHandle, session_id: String) -> Result<()> {
    let view = engine_view::read().await?;
    let row = engine_view::own_claude_row(&view, &session_id).ok_or("that session is not running on this Mac")?;
    let state = state_dir(&app)?;
    if !sharing_on(&state) {
        return Err("sharing is off on this Mac".into());
    }
    // Before the dialog: an ask for a session the relay does not hold yet would wait for it and
    // apply later, with no new Allow.
    let key = transport::claude_key(&home()?, &row.conversation);
    let live = || -> Result<Mirror> {
        if !sharing_on(&state) {
            return Err("sharing is off on this Mac".into());
        }
        super::mod_socket::mirrors(&session_scope(&state)?)
            .into_iter()
            .find_map(|(k, m)| (k == key && m.ended_at.is_none()).then_some(m))
            .ok_or("Lightbulb does not see this session yet. Wait a few seconds, then try again.".into())
    };
    live()?;
    if !crate::confirm_native(&app, "Share this session with your workspace?", engine_view::share_prompt(row)).await {
        return Err(crate::DECLINED_ON_MAC.into());
    }
    // The dialog can stay open for minutes: sharing may have gone off or the session ended.
    let mirror = live()?;
    let enrollment: Enrollment = private_read(&state.join("enrollment.json"))?;
    let client = reqwest::Client::builder().timeout(std::time::Duration::from_secs(8)).build().map_err(|_| "network unavailable")?;
    super::mod_socket::share(&client, &enrollment, &load_mac_identity()?, &key, &mirror)
        .await
        .map_err(|e| e["message"].as_str().unwrap_or("could not share").to_owned())
}

/// The workspace this Mac's sessions are shared to, if enrolled.
pub(crate) fn enrolled_owner(state_dir: &Path) -> Option<String> {
    private_read::<Enrollment>(&state_dir.join("enrollment.json")).ok().map(|e| e.owner_id)
}
pub(crate) fn scope_key(enrollment: &Enrollment) -> String {
    fingerprint(&json!([enrollment.owner_id, enrollment.human_user_id, enrollment.device_id]).to_string())
}

pub(crate) fn launch_scope(state_dir: &Path) -> Result<String> {
    let enrollment: Enrollment = private_read(&state_dir.join("enrollment.json"))?;
    if !enrollment.enabled || enrollment.paused || enrollment.remote_disabled {
        return Err("session sharing is off".into());
    }
    Ok(scope_key(&enrollment))
}

/// The dir of this Mac's shared-session mirrors (native_tools reads it), while sharing is on.
pub(crate) fn session_scope(state_dir: &Path) -> Result<PathBuf> {
    Ok(state_dir.join(launch_scope(state_dir)?))
}
/// `lightbulb pair`'s write: the same private enrollment.json the window's enable writes.
pub(crate) fn save_enrollment(state_dir: &Path, enrollment: &Enrollment) -> Result<()> {
    private_write(&state_dir.join("enrollment.json"), enrollment)
}
/// The one Tauri wrapper: the window's local-sessions dir.
pub(crate) fn state_dir(app: &tauri::AppHandle) -> Result<PathBuf> {
    local_state_dir(&app.path().app_data_dir().map_err(|_| "desktop storage unavailable")?)
}
/// The window's app data dir and its current Clerk token (empty before sign-in).
fn app_auth(app: &tauri::AppHandle) -> Result<(PathBuf, String)> {
    let data = app.path().app_data_dir().map_err(|_| "desktop storage unavailable")?;
    let jwt = app
        .try_state::<crate::AuthState>()
        .and_then(|s| s.current_jwt.read().ok().map(|j| j.clone()))
        .unwrap_or_default();
    Ok((data, jwt))
}
/// `<app data>/local-sessions`, created private.
pub(crate) fn local_state_dir(app_dir: &Path) -> Result<PathBuf> {
    fs::create_dir_all(app_dir).map_err(|_| "desktop storage unavailable")?;
    let dir = app_dir.join("local-sessions");
    private_dir(&dir)?;
    Ok(dir)
}
pub(super) fn hook_path() -> PathBuf {
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

/// The one descriptor a hook must never be able to speak on: its stdout, the
/// only channel Claude reads a permission decision from.
const HOOK_DECISION_FD: std::os::unix::io::RawFd = 1;

/// A hook's stdout is a control channel: Claude reads it for a
/// `PermissionRequest`/`PreToolUse` decision (`allow` / `deny` / `ask`) and for
/// `continue: false`. Ours only reports what a session is waiting on, so the
/// descriptor is pointed at /dev/null before a byte is read -- no future log
/// line, and nothing a library prints, can answer a prompt on the user's behalf.
///
/// Takes the descriptor rather than assuming 1 so the guard can be proved on one
/// a test owns; production always passes [`HOOK_DECISION_FD`].
fn silence(fd: std::os::unix::io::RawFd) {
    use std::os::unix::io::AsRawFd;
    if let Ok(devnull) = fs::OpenOptions::new().write(true).open("/dev/null") {
        // Replaces the descriptor rather than closing it: a closed fd 1 would be
        // handed to the next file this process opens.
        unsafe { libc::dup2(devnull.as_raw_fd(), fd) };
    }
}

/// Every session's hook sends the bounded lifecycle datagram built from their stdin JSON, or
/// a plain wake byte when stdin isn't a JSON object; never a credential or an
/// authority. The one exception to "never a decision" is a Claude permission
/// prompt the app holds for a remote answer (owner ruling 2026-09-25): that
/// path keeps a private copy of stdout and writes to it only a decision
/// `approval::hold` built for that exact prompt. Every other path loses stdout
/// before a byte of stdin is read (see [`silence`]).
pub fn hook(provider: &str) {
    let answer = unsafe { libc::dup(HOOK_DECISION_FD) };
    silence(HOOK_DECISION_FD);
    let mut raw = Vec::new();
    let _ = std::io::Read::take(std::io::stdin(), 65536).read_to_end(&mut raw);
    let json: serde_json::Value = serde_json::from_slice(&raw).unwrap_or(serde_json::Value::Null);
    if super::hooked::provider(provider) {
        // Our hook command names the event (`… local-session-hook cursor stop`).
        let event = std::env::args().nth(3).unwrap_or_default();
        let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_default();
        let source = super::lifecycle::hook_process(provider);
        return hooked_hook(provider, &event, json, answer, &hook_path(), &home, source.as_ref());
    }
    let holders = if provider == "codex" && json["hook_event_name"] == "SessionStart" {
        crate::holder::manager::list()
    } else {
        vec![]
    };
    hook_input(provider, json, std::os::unix::process::parent_id(), &holders, answer, &hook_path());
}

/// A Cursor or Grok CLI hook (agent network phase 2): it registers its session, wakes the app,
/// and, on a hook whose output reaches the model, hands over the letters that wait for the session.
fn hooked_hook(provider: &str, event: &str, json: serde_json::Value, answer: std::os::unix::io::RawFd, wake: &Path, home: &Path, source: Option<&super::lifecycle::HookProcess>) {
    let close = |fd: std::os::unix::io::RawFd| if fd >= 0 { unsafe { libc::close(fd) }; };
    let Some(json) = super::hooked::normalize(provider, event, json) else { return close(answer) };
    let dir = wake.with_file_name("hooked");
    let _ = super::hooked::record(&dir, provider, &json, source, home);
    // Before the datagram, which may start discovery of this session.
    send_hook_datagram(wake, provider, &json);
    let canonical = json["hook_event_name"].as_str().unwrap_or("");
    let session = json["session_id"].as_str().unwrap_or("");
    if answer < 0 || !super::hooked::takes_mail(provider, canonical) {
        return close(answer);
    }
    let lines = super::hooked::drain(&dir, provider, session);
    if lines.is_empty() {
        return close(answer);
    }
    use std::io::Write;
    use std::os::unix::io::FromRawFd;
    // SAFETY: `answer` is this hook's own dup of stdout, owned and closed here.
    let mut stdout = unsafe { fs::File::from_raw_fd(answer) };
    let _ = stdout.write_all(super::hooked::output(provider, canonical, &lines.join("\n\n")).to_string().as_bytes());
}

/// One hook after its stdin is read; `parent` is the hook's parent process.
fn hook_input(
    provider: &str,
    json: serde_json::Value,
    parent: u32,
    holders: &[HolderInfo],
    answer: std::os::unix::io::RawFd,
    wake: &Path,
) {
    if answer >= 0 && super::approval::holdable(provider, &json) {
        super::approval::hold(wake, provider, &json, answer);
        return;
    }
    if answer >= 0 {
        unsafe { libc::close(answer) };
    }
    // Codex's PostToolUse hook told the agent about held requests, which no longer exist (agents
    // message through the engine); an installed one says nothing and is not a lifecycle event.
    if provider == "codex" && json["hook_event_name"] == "PostToolUse" {
        return;
    }
    // Discovery names a held Codex by the thread its own start hook records here.
    if provider == "codex" && json["hook_event_name"] == "SessionStart" {
        if let (Some(holder), Some(thread)) = (hook_holder(parent, holders), json["session_id"].as_str()) {
            crate::holder::manager::bind_thread(holder, thread);
        }
    }
    // This write precedes the optional datagram: Desktop may be closed when
    // the provider starts, but its exact native conversation still needs a link.
    let _ = super::start_links::record_from_hook(provider, &json);
    send_hook_datagram(wake, provider, &json);
}

/// The Codex holder of the provider that ran this hook: the hook's parent, or the parent's parent
/// when the provider ran it through a shell (`sh -c '<hook command>'`). Never another provider's
/// holder (`codex_holder`).
fn hook_holder(parent: u32, holders: &[HolderInfo]) -> Option<&HolderInfo> {
    let shell = process(parent).ok()?.executable.file_name().is_some_and(|name| {
        matches!(name.to_str(), Some("sh" | "bash" | "zsh" | "dash"))
    });
    let provider = if shell { super::native_tools::parent(parent).ok()?.pid } else { parent };
    holder_of(provider, holders).filter(|h| codex_holder(h))
}

/// Split out from [`hook`] so tests can point it at a temp socket instead of
/// the real hook path.
pub(crate) fn send_hook_datagram(path: &Path, provider: &str, json: &serde_json::Value) {
    if private_dir(path.parent().unwrap()).is_err() {
        return;
    }
    let bytes = if json.is_object() {
        super::lifecycle::build_datagram(provider, json)
    } else {
        b"1".to_vec()
    };
    if let Ok(socket) = std::os::unix::net::UnixDatagram::unbound() {
        let _ = socket.set_write_timeout(Some(std::time::Duration::from_millis(100)));
        let _ = socket.send_to(&bytes, path);
    }
}

#[tauri::command]
pub async fn local_sessions_auth(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
    human_user_id: Option<String>,
) -> Result<()> {
    // Every page load reports this. The same person with sharing already running changes
    // nothing: restarting here dropped the relay connection and every teammate's live view.
    if state.sharing_as(human_user_id.as_deref()) {
        return Ok(());
    }
    // Invalidate in-flight enrollment before waiting for its network request.
    {
        let mut human = state
            .human
            .lock()
            .map_err(|_| "sharing state unavailable")?;
        state.revision.fetch_add(1, Ordering::SeqCst);
        // Signed out: forget the page's workspace name for the `lightbulb` command's line.
        #[cfg(unix)]
        if human_user_id.is_none() {
            crate::cli::set_workspace(None);
        }
        *human = Some(human_user_id);
        state.stop();
    }
    state.restore(&app).await.map_err(|e| {
        crate::log(&format!("[local-sessions] auth restore failed: {e}"));
        e
    })
}

/// Who watches this run, as counts: the owner's own app shows them over its terminal as the web
/// terminal does (`SessionTerminalView`), read from the presence the band draws. No network.
#[tauri::command]
pub fn local_session_audience(session_id: String, generation: String) -> Value {
    let entry = super::presence::get(&session_id, &generation);
    json!({"watching": entry.as_ref().map_or(0, |e| e.viewers.len()), "viaLink": entry.map_or(0, |e| e.link_viewers)})
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
        "pairedWorkspaceId":paired.map(|p|p.owner_id),"status":state.status.lock().map_err(|_|"sharing state unavailable")?.clone(),
        "providers":provider_status(&home()?, &std::env::current_exe().map_err(|_| "desktop executable unavailable")?)}),
    )
}

/// What each provider would actually load in a new session, read from disk only.
fn provider_status(home: &Path, executable: &Path) -> Value {
    let hooks = |provider: &str| {
        if provider == "claude" {
            claude::hooks_state(&home.join(".claude/settings.json"), executable)
        } else {
            codex::hooks_state(home, executable)
        }
    };
    let row = |provider: &str| json!({
        "cli": super::provider_program(home, provider).is_some(),
        "mcp": super::native_mcp::status(home, provider),
        "hooks": hooks(provider),
    });
    json!({"claude": row("claude"), "codex": row("codex")})
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
    // A viewer enrolls the same way but starts no run: `serve` picks up enrollment.json.
    let serving = state.window_serves(
        &app.path().app_data_dir().map_err(|_| "desktop storage unavailable")?,
        &crate::headless::launch_agent_path(),
    )?;
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
        let mut codex_notes: Vec<String> = Vec::new();
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
            // Codex removal is best effort: its hooks are inert once this
            // executable stops answering, and an absent or hand-edited Codex
            // config must not be reported as a Claude settings failure.
            if let Err(reason) = codex::configure_hooks(&home()?, &installed, false) {
                codex_notes.push(format!("Codex hooks were not removed ({reason})."));
            }
            codex_notes.extend(
                super::native_mcp::configure(&home()?, &installed, false)
                    .into_iter()
                    .chain(super::hooked::configure(&home()?, &installed, false))
                    .map(|note| format!("{note}.")),
            );
            Ok(())
        })?;
        // Skill removal is tracked separately from hook cleanup so a failure in
        // one is never misattributed to the other in the status message.
        let skill_removed = home().and_then(|h| remove_skill(&dir, &h)).is_ok();
        if let Ok(h) = home() {
            super::plugin::remove(&dir, &h);
        }
        let mut parts = Vec::new();
        if !cleaned {
            parts.push("Local sharing stopped; Claude wake-hook removal failed. Retry disable after repairing Claude settings.".to_owned());
        }
        if !skill_removed {
            parts.push("Local sharing stopped; the lightbulb-session-sharing skill could not be removed from ~/.claude/skills or ~/.codex/skills. Delete it by hand.".to_owned());
        }
        if !codex_notes.is_empty() {
            if parts.is_empty() {
                parts.push("Local sharing stopped; Codex cleanup was incomplete.".to_owned());
            }
            parts.extend(codex_notes);
        }
        if !parts.is_empty() {
            *state
                .status
                .lock()
                .map_err(|_| "sharing state unavailable")? = parts.join(" ");
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
        // Distinct from the never-signed-in case above: the Settings click
        // arrived before the webview reported this user (or for another
        // user). The frontend re-reports auth before calling here; reaching
        // this means that report has not landed yet.
        return Err("Desktop sign-in report is stale. Wait a moment and try again.".into());
    }
    let identity = crate::keychain::load_or_generate(
        crate::keychain::KeychainSlot::Mac,
        crate::KEYCHAIN_SERVICE,
    )?;
    let app_data = app.path().app_data_dir().map_err(|_| "desktop storage unavailable")?;
    let pair = retained_pair(crate::pair_state::load(&app_data), &identity.public_key_pem)?;
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
    let token = token
        .as_deref()
        .ok_or("Sign-in token unavailable. Sign in again and retry enabling.")?;
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
            if installed != executable {
                // Codex hooks naming a moved executable would run a dead path.
                if let Ok(home) = home() {
                    let _ = codex::configure_hooks(&home, &installed, false);
                    let _ = super::native_mcp::configure(&home, &installed, false);
                    let _ = super::hooked::configure(&home, &installed, false);
                }
            }
            if serving {
                state.prepare(&app_data, &enrollment).map(Some)
            } else {
                Ok(None)
            }
        },
        |prepared, active| state.finish_enable(&path, active, prepared, revision),
    )
    .await?;
    // Codex hooks and the skill are reported, never fatal: the Claude path that
    // just came up does not depend on either.
    let notes = reassert(&dir);
    if !notes.is_empty() {
        *state.status.lock().map_err(|_| "sharing state unavailable")? = format!(
            "Sharing enabled; {}. Claude/Codex still receive labelled teammate messages.",
            notes.join("; ")
        );
    }
    Ok(revision)
}

/// Re-run hook, MCP and skill setup for an enabled enrollment without touching
/// the enrollment itself (re-enabling would retire it). Returns the failures.
#[tauri::command]
pub async fn local_sessions_repair(
    app: tauri::AppHandle,
    state: tauri::State<'_, Controller>,
) -> Result<Vec<String>> {
    let _guard = state.mutation.lock().await;
    let dir = state_dir(&app)?;
    let path = dir.join("enrollment.json");
    if !path.try_exists().map_err(|_| "sharing state unavailable")?
        || !private_read::<Enrollment>(&path)?.enabled
    {
        return Err("Enable local-session sharing first".into());
    }
    // Retry also re-asks `/desktop/pair` for the relay door and, when it moved,
    // restarts the relay on the new one.
    state.relay_checked.store(false, Ordering::SeqCst);
    let (data, jwt) = app_auth(&app)?;
    let moved = state.refresh_gateway_url(&data, &jwt, &path).await;
    let notes = reassert(&dir);
    *state.status.lock().map_err(|_| "sharing state unavailable")? = retried(&notes);
    drop(_guard);
    if moved {
        state.restore(&app).await?;
    }
    Ok(notes)
}

/// Every retry replaces the last one's status, so a fixed failure stops showing.
fn retried(notes: &[String]) -> String {
    if notes.is_empty() {
        "Setup retried; Claude Code, Codex, Cursor and Grok CLI are configured for new sessions.".into()
    } else {
        format!("Setup retried; {}.", notes.join("; "))
    }
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

pub(crate) const SKILL_MD: &str = include_str!("../../../../../packages/claude-plugin/lightbulb/skills/lightbulb-session-sharing/SKILL.md");
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
    // Cursor reads `~/.cursor/skills`; Grok CLI has no user skill root we know of.
    for root in [".claude", ".codex", ".cursor"] {
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

/// Re-assert both providers' hooks and the bundled skill for an enrollment that
/// is already enabled. Providers are installed, upgraded and hand-edited between
/// launches, so a resume rewrites them rather than trusting the install that
/// consent performed. Returns the failures for the status line; none is fatal.
fn reassert(state_dir: &Path) -> Vec<String> {
    match std::env::current_exe() {
        Ok(executable) => reassert_exe(state_dir, &executable),
        Err(_) => vec!["the desktop executable is unavailable".into()],
    }
}

/// `reassert` naming `executable` in the hooks: the CLI passes the resolved app binary, since
/// it may run through the `~/.local/bin/lightbulb` link, which is not a hook command.
pub(crate) fn reassert_exe(state_dir: &Path, executable: &Path) -> Vec<String> {
    let home = match home() {
        Ok(home) => home,
        Err(reason) => return vec![reason],
    };
    let mut notes = reassert_at(state_dir, &home, executable);
    if let Err(reason) = super::plugin::install(state_dir, &home) {
        notes.push(format!("the lightbulb Claude plugin was not installed ({reason})"));
    }
    notes
}

fn reassert_at(state_dir: &Path, home: &Path, executable: &Path) -> Vec<String> {
    let record = state_dir.join("hook-executable.json");
    let installed: PathBuf = private_read(&record).unwrap_or_else(|_| executable.to_owned());
    let moved = installed != executable;
    let settings = home.join(".claude/settings.json");
    let claude = settings.parent().is_some_and(|parent| parent.is_dir());
    // The app can move between launches. Hooks naming the old path are dead, so
    // a resume heals them at the current path instead of cementing the stale
    // install -- the same reconcile consent performs.
    if moved {
        if claude {
            let _ = claude::configure_hooks(&settings, &installed, false);
        }
        let _ = codex::configure_hooks(home, &installed, false);
        let _ = super::native_mcp::configure(home, &installed, false);
        let _ = super::hooked::configure(home, &installed, false);
    }
    let mut notes = Vec::new();
    if claude {
        if let Err(reason) = claude::configure_hooks(&settings, executable, true) {
            notes.push(format!("Claude hooks were not installed ({reason})"));
        }
    }
    if let Err(reason) = codex::configure_hooks(home, executable, true) {
        notes.push(format!("Codex hooks were not installed ({reason})"));
    }
    // hooked.rs first: native_mcp takes out the `session-mcp` entry it writes for Cursor and Grok.
    notes.extend(super::hooked::configure(home, executable, true));
    notes.extend(super::native_mcp::configure(home, executable, true));
    if moved {
        if let Err(reason) = private_write(&record, &executable) {
            notes.push(format!("the hook executable record was not updated ({reason})"));
        }
    }
    if let Err(reason) = install_skill(state_dir, home) {
        notes.push(format!(
            "the lightbulb-session-sharing skill was not installed ({reason})"
        ));
    }
    notes
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

    #[test]
    fn audience_counts_this_runs_link_viewers_and_nothing_for_another_run() {
        use super::super::presence;
        let _g = presence::TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        const SID: &str = "a0d1e2c3-0000-4000-8000-0000000000a1";
        presence::set(SID, presence::Entry { generation: "g1".into(), paused: false, viewers: vec![], join_requests: vec![], link_viewers: 3, at: 0 });
        assert_eq!(local_session_audience(SID.into(), "g1".into()), json!({"watching": 0, "viaLink": 3}));
        assert_eq!(local_session_audience(SID.into(), "g2".into()), json!({"watching": 0, "viaLink": 0}));
    }

    #[test]
    fn mod_socket_and_native_tools_serve_while_every_shared_worker_is_blocked() {
        use std::sync::atomic::AtomicUsize;
        use std::time::{Duration, Instant};
        // Fill every worker of the shared runtime with blocking work, as the prod sample showed.
        let workers = std::thread::available_parallelism().map_or(1, |n| n.get());
        let parked = Arc::new(AtomicUsize::new(0));
        let release = Arc::new(std::sync::Mutex::new(()));
        let held = release.lock().unwrap();
        for _ in 0..workers {
            let (parked, release) = (parked.clone(), release.clone());
            tauri::async_runtime::spawn(async move {
                parked.fetch_add(1, Ordering::SeqCst);
                drop(release.lock());
            });
        }
        let started = Instant::now();
        while parked.load(Ordering::SeqCst) < workers && started.elapsed() < Duration::from_secs(10) {
            std::thread::yield_now();
        }
        assert_eq!(parked.load(Ordering::SeqCst), workers, "shared runtime was not filled");
        let (tx, rx) = std::sync::mpsc::channel();
        let _sockets = serve_sockets(vec![Box::pin(async move {
            let _ = tx.send(());
        })]);
        let served = rx.recv_timeout(Duration::from_secs(5)).is_ok();
        drop(held);
        assert!(served, "a socket server waited on the shared runtime");
    }

    #[test]
    fn local_sessions_hook_holder_resolves_through_the_hooks_shell() {
        use std::time::{Duration, Instant};
        let sh = |cmd: &str| std::process::Command::new("/bin/sh").args(["-c", cmd]).spawn().unwrap();
        let children = |pid: u32| -> Vec<u32> {
            let out = std::process::Command::new("/usr/bin/pgrep").args(["-P", &pid.to_string()]).output().unwrap();
            String::from_utf8_lossy(&out.stdout).lines().filter_map(|l| l.trim().parse().ok()).collect()
        };
        // Outer shell = provider; inner shell = the hook's parent. Compound so the inner stays a shell.
        let mut outer = sh(r#"/bin/sh -c '/bin/sleep 30; :'; :"#);
        let deadline = Instant::now() + Duration::from_secs(5);
        let inner = loop {
            if let Some(pid) = children(outer.id()).first() {
                break *pid;
            }
            assert!(Instant::now() < deadline, "the provider started its shell");
            std::thread::sleep(Duration::from_millis(20));
        };
        let dir = tempfile::tempdir().unwrap();
        let holder = HolderInfo {
            id: "ab12cd34".into(),
            socket: dir.path().join("h.sock"),
            pid: std::process::id(),
            provider_pid: outer.id(),
            argv: vec!["/opt/homebrew/bin/codex".into()],
            cwd: "/".into(),
            watch: false,
            type_input: false,
        };
        let found = hook_holder(inner, std::slice::from_ref(&holder)).map(|h| h.id.clone());
        let other = HolderInfo { provider_pid: outer.id() + 1, ..holder.clone() };
        let none = hook_holder(inner, std::slice::from_ref(&other)).is_none();
        // The same hook from a shell inside a held Claude never names that Claude's holder.
        let claude = HolderInfo { argv: vec!["/Users/a/.local/share/claude/versions/2.1.306".into()], ..holder.clone() };
        let not_codex = hook_holder(inner, std::slice::from_ref(&claude)).is_none();
        for pid in children(inner).into_iter().chain([inner]) {
            unsafe { libc::kill(pid as i32, libc::SIGKILL) };
        }
        let _ = outer.kill();
        let _ = outer.wait();
        assert_eq!(found.as_deref(), Some("ab12cd34"));
        assert!(not_codex, "a Claude holder is never bound to a Codex thread");
        assert!(none, "a holder whose provider is another process does not match");
    }

    /// Codex's PostToolUse hook (installed for the retired held-request notice) writes nothing
    /// and sends no lifecycle datagram.
    #[test]
    fn local_sessions_codex_post_tool_use_hook_says_nothing() {
        use std::io::Read;
        use std::os::unix::io::FromRawFd;
        let dir = tempfile::tempdir().unwrap();
        let wake = dir.path().join("wake.sock");
        let app = std::os::unix::net::UnixDatagram::bind(&wake).unwrap();
        app.set_nonblocking(true).unwrap();
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        let stdin = json!({"hook_event_name":"PostToolUse","session_id":"thread-1","tool_name":"shell"});
        hook_input("codex", stdin, std::process::id(), &[], fds[1], &wake);
        let mut out = String::new();
        unsafe { fs::File::from_raw_fd(fds[0]) }.read_to_string(&mut out).unwrap();
        assert_eq!(out, "");
        assert!(app.recv(&mut [0u8; 64]).is_err(), "PostToolUse is not a lifecycle event");
    }

    /// Black box over hooked.rs's own Cursor and Grok writer: whatever it writes, a launch ends with
    /// no `session-mcp` entry, and another program's `lightbulb` (the engine's) stays.
    #[test]
    fn cursor_and_grok_fixture_has_no_session_mcp_after_launch() {
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let files = |h: &Path| [h.join(".cursor/mcp.json"), h.join(".grok/config.toml")].map(|p| fs::read_to_string(p).unwrap_or_default());
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        fs::create_dir_all(h.join(".cursor")).unwrap();
        fs::create_dir_all(h.join(".grok")).unwrap();
        let state = tempfile::tempdir().unwrap();
        // The fixture is not vacuous: hooked.rs on its own writes our entry into both.
        let _ = super::super::hooked::configure(h, exe, true);
        for (i, text) in files(h).iter().enumerate() {
            assert!(text.contains("session-mcp"), "file {i}: {text}");
        }
        let _ = reassert_at(state.path(), h, exe);
        for (i, text) in files(h).iter().enumerate() {
            assert!(!text.contains("session-mcp"), "file {i} after launch: {text}");
        }
        // The engine's entry is someone else's: a launch keeps it.
        let engine = json!({"mcpServers":{"lightbulb":{"command":"/Users/x/.lightbulb/connect/bin/lightbulb","args":["mcp"]}}});
        fs::write(h.join(".cursor/mcp.json"), engine.to_string()).unwrap();
        fs::write(h.join(".grok/config.toml"), "[mcp_servers.lightbulb]\ncommand = \"/Users/x/.lightbulb/connect/bin/lightbulb\"\nargs = [\"mcp\"]\n").unwrap();
        let _ = reassert_at(state.path(), h, exe);
        let [cursor, grok] = files(h);
        assert_eq!(serde_json::from_str::<Value>(&cursor).unwrap()["mcpServers"]["lightbulb"], engine["mcpServers"]["lightbulb"]);
        assert!(grok.contains("/Users/x/.lightbulb/connect/bin/lightbulb") && !grok.contains("session-mcp"), "{grok}");
    }

    /// Agent network phase 2: a Cursor or Grok CLI hook registers its session, wakes the app with
    /// a Claude-shaped datagram, and hands a waiting letter over only on a hook the model reads.
    #[test]
    fn local_sessions_hooked_agent_hook_registers_and_hands_over_its_letters() {
        use std::io::Read;
        use std::os::unix::fs::PermissionsExt;
        use std::os::unix::io::FromRawFd;
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let wake = dir.path().join("wake.sock");
        let app = std::os::unix::net::UnixDatagram::bind(&wake).unwrap();
        app.set_nonblocking(true).unwrap();
        let hooked = dir.path().join("hooked");
        let run = |provider: &str, event: &str, stdin: Value| -> String {
            let mut fds = [0; 2];
            assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
            // The agent stands in as this test process: its real ancestors are cargo's.
            let me = super::super::process(std::process::id()).unwrap();
            let source = super::super::lifecycle::HookProcess::of(&me);
            hooked_hook(provider, event, stdin, fds[1], &wake, Path::new("/Users/a"), Some(&source));
            let mut out = String::new();
            unsafe { fs::File::from_raw_fd(fds[0]) }.read_to_string(&mut out).unwrap();
            out
        };
        for (provider, id) in [("cursor", "conv-1"), ("grok", "grok-1")] {
            assert_eq!(run(provider, "session-start", json!({"session_id": id, "cwd": "/w/api"})), "");
            let mut buf = [0u8; 2048];
            let n = app.recv(&mut buf).unwrap();
            let event = super::super::lifecycle::parse_datagram(&buf[..n]).expect("a datagram the app reads");
            assert_eq!((event.provider.as_str(), event.event.as_str(), event.session_id.as_str()), (provider, "SessionStart", id));
        }
        let sessions = super::super::hooked::discover(&hooked);
        assert_eq!(sessions.len(), 2, "each exists from its first hook");
        for s in &sessions {
            super::super::hooked::deliver(&hooked, s, "r-1", "[Lightbulb teammate contribution] hi", None).unwrap();
        }
        // A prompt hook cannot add context: the letter stays for the next tool call.
        assert_eq!(run("cursor", "user-prompt-submit", json!({"conversation_id": "conv-1"})), "");
        let out: Value = serde_json::from_str(&run("cursor", "post-tool-use", json!({"conversation_id": "conv-1"}))).unwrap();
        assert_eq!(out, json!({"additional_context": "[Lightbulb teammate contribution] hi"}));
        assert_eq!(run("cursor", "stop", json!({"conversation_id": "conv-1"})), "", "handed over once");
        // Grok's Stop counts only at a turn's end, and its SessionStart output is dropped.
        assert_eq!(run("grok", "stop", json!({"session_id": "grok-1", "reason": "shutdown"})), "");
        let out: Value = serde_json::from_str(&run("grok", "stop", json!({"session_id": "grok-1", "reason": "end_turn"}))).unwrap();
        assert_eq!(out, json!({"decision": "block", "reason": "[Lightbulb teammate contribution] hi"}));
    }

    #[test]
    fn local_sessions_a_plain_sessions_hook_reaches_the_app() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let wake = dir.path().join("wake.sock");
        let app = std::os::unix::net::UnixDatagram::bind(&wake).unwrap();
        app.set_nonblocking(true).unwrap();
        // This test process is a plain terminal's `claude`: no holder anywhere.
        hook_input("claude", json!({"hook_event_name":"Stop","session_id":"s"}), std::process::id(), &[], -1, &wake);
        assert!(app.recv(&mut [0u8; 4096]).is_ok(), "every session's hook reaches the app (rulings 2026-10-04)");
    }

    #[test]
    fn local_sessions_a_held_codex_start_binds_its_thread_and_wakes_the_app() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let wake = dir.path().join("wake.sock");
        let app = std::os::unix::net::UnixDatagram::bind(&wake).unwrap();
        app.set_nonblocking(true).unwrap();
        // This test process stands in for the holder; its direct child is the held Codex.
        let mut codex = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let holder = HolderInfo {
            id: "ab12cd34".into(),
            socket: dir.path().join("ab12cd34.sock"),
            pid: std::process::id(),
            provider_pid: codex.id(),
            argv: vec!["/opt/homebrew/bin/codex".into()],
            cwd: "/".into(),
            watch: false,
            type_input: false,
        };
        let thread = "11111111-1111-4111-8111-111111111111";
        hook_input("codex", json!({"hook_event_name":"SessionStart","session_id":thread}),
            codex.id(), std::slice::from_ref(&holder), -1, &wake);
        assert_eq!(crate::holder::manager::thread(&holder).as_deref(), Some(thread));
        assert!(app.recv(&mut [0u8; 4096]).is_ok(), "the start hook still wakes discovery");
        codex.kill().unwrap();
        codex.wait().unwrap();
    }
    #[test]
    fn window_takes_the_lock_only_without_a_launch_agent() {
        let data = tempfile::tempdir().unwrap();
        let agent = data.path().join("company.thelightbulb.serve.plist");
        fs::write(&agent, "plist").unwrap();
        let window = Controller::default();
        assert!(!window.window_serves(data.path(), &agent).unwrap());
        assert!(crate::headless::service_lock(data.path()).unwrap().is_some(), "the window left serve.lock to serve");
        fs::remove_file(&agent).unwrap();
        assert!(window.window_serves(data.path(), &agent).unwrap());
        assert!(crate::headless::service_lock(data.path()).unwrap().is_none(), "the window holds serve.lock");
    }

    #[test]
    fn window_hands_the_lock_to_a_launch_agent_installed_while_it_runs() {
        let data = tempfile::tempdir().unwrap();
        let agent = data.path().join("company.thelightbulb.serve.plist");
        let window = Controller::default();
        assert!(window.window_serves(data.path(), &agent).unwrap(), "no agent yet: the window serves");
        assert!(!tauri::async_runtime::block_on(window.hand_over_to_agent(data.path(), &agent)));
        assert!(crate::headless::service_lock(data.path()).unwrap().is_none(), "still the window's");
        fs::write(&agent, "plist").unwrap(); // `lightbulb pair` installs it
        assert!(tauri::async_runtime::block_on(window.hand_over_to_agent(data.path(), &agent)));
        assert!(window.running.lock().unwrap().is_none(), "stopped before the lock was let go");
        assert_eq!(*window.status.lock().unwrap(), "Local sharing runs in lightbulb-desktop serve.");
        assert!(crate::headless::service_lock(data.path()).unwrap().is_some(), "serve can take serve.lock");
        assert!(!tauri::async_runtime::block_on(window.hand_over_to_agent(data.path(), &agent)), "once");
    }

    #[test]
    fn restore_with_no_change_writes_nothing() {
        use std::os::unix::fs::MetadataExt;
        let data = tempfile::tempdir().unwrap();
        let dir = local_state_dir(data.path()).unwrap();
        let path = dir.join("enrollment.json");
        private_write(&path, &candidate()).unwrap();
        let before = fs::metadata(&path).unwrap().ino();
        let window = Controller::default();
        tauri::async_runtime::block_on(window.restore_at(data.path(), "")).unwrap();
        assert_eq!(fs::metadata(&path).unwrap().ino(), before, "an unchanged enrollment is not rewritten");
    }

    #[test]
    fn viewer_toggle_writes_enrollment() {
        let dir = tempfile::tempdir().unwrap();
        private_dir(&dir.path().join("ls")).unwrap();
        let path = dir.path().join("ls/enrollment.json");
        let window = Controller::default();
        *window.human.lock().unwrap() = Some(Some("alice".into()));
        let mut active = candidate();
        active.enabled = true;
        active.attempt = None;
        window.finish_enable(&path, active.clone(), None, 0).unwrap();
        assert_eq!(private_read::<Enrollment>(&path).unwrap(), active);
        assert!(window.running.lock().unwrap().is_none(), "a viewer starts no run");
        assert!(sharing_on(&dir.path().join("ls")));
    }

    fn candidate() -> Enrollment {
        Enrollment {
            owner_id: "org".into(),
            human_user_id: "alice".into(),
            device_id: "mac".into(),
            gateway_url: "wss://example.invalid".into(),
            enabled: false,
            paused: false,
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
            scope: dir.join("host"),
        }
    }
    #[tokio::test]
    async fn local_sessions_hook_binary_datagram_reaches_listener() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        // `private_dir` requires an existing parent to already be 0700; unlike
        // a fresh subdir it won't fix a pre-existing tempdir's mode for us.
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        let path = dir.path().join("wake.sock");
        let socket = tokio::net::UnixDatagram::bind(&path).unwrap();
        let stdin = serde_json::json!({"session_id":"abc","transcript_path":"/t","cwd":"/w","hook_event_name":"Stop","last_assistant_message":"hi"});
        send_hook_datagram(&path, "claude", &stdin);
        let mut buf = [0u8; 2048];
        let n = socket.recv(&mut buf).await.unwrap();
        let event = super::super::lifecycle::parse_datagram(&buf[..n]).unwrap();
        assert_eq!(event.session_id, "abc");
        assert_eq!(event.last_assistant_message.as_deref(), Some("hi"));
    }
    #[tokio::test]
    async fn local_sessions_auth_invalidation_at_publication_keeps_disk_stopped() {
        // Third element is the paused outcome `authenticate` must produce on the
        // disk-persisted (pre-commit) row below, independent of the revision
        // needed to force `publish` to reject this attempt.
        for (human, revision, expect_paused) in [
            (None, 0, false), // unreported: unchanged from candidate()'s paused:false
            (Some(None), 0, true),
            (Some(Some("bob".into())), 0, true),
            (Some(Some("alice".into())), 1, false),
        ] {
            let dir = tempfile::tempdir().unwrap();
            let path = dir.path().join("enrollment.json");
            let state = Controller::default();
            *state.human.lock().unwrap() = human.clone();
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
            restored.authenticate(human.as_ref().map(|h| h.as_deref()));
            assert!(!restored.enabled && restored.attempt.is_some());
            assert_eq!(restored.paused, expect_paused);
            assert!(state.running.lock().unwrap().is_none());
            // Launch restore reads the same table. An unreported human is a
            // quiet no-op; every other state still has to pass
            // `require_current`, which the bumped revision forces to reject.
            assert_eq!(
                state
                    .resume(candidate(), prepared(dir.path()), revision + 1)
                    .is_ok(),
                human.is_none(),
                "restore must not fail merely because nobody has reported the human yet"
            );
            assert!(state.running.lock().unwrap().is_none());
        }
    }
    #[tokio::test]
    async fn local_sessions_a_page_load_by_the_same_person_keeps_the_running_relay() {
        // Every page load reports who is signed in. That report stopped and restarted the
        // relay connection with no log line (prod 2026-10-07 15:44:38Z, 20 of 20 page loads).
        let state = Controller::default();
        *state.human.lock().unwrap() = Some(Some("alice".into()));
        assert!(!state.sharing_as(Some("alice")), "nothing runs yet: the report has to start it");
        *state.running.lock().unwrap() = Some(Running {
            stop: Arc::new(AtomicBool::new(false)),
            task: tauri::async_runtime::spawn(std::future::pending()),
        });
        assert!(state.sharing_as(Some("alice")));
        assert!(!state.sharing_as(Some("bob")), "another person: sharing stops");
        assert!(!state.sharing_as(None), "signed out: sharing stops");
        state.stop();
        assert!(!state.sharing_as(Some("alice")));
    }
    #[test]
    fn local_sessions_authenticate_treats_unreported_as_unchanged() {
        let mut e = candidate(); // human_user_id "alice", paused false
        e.authenticate(None);
        assert!(!e.paused);
        e.authenticate(Some(None));
        assert!(e.paused);
        e.authenticate(None);
        assert!(e.paused, "unreported never un-pauses either");
        e.authenticate(Some(Some("alice")));
        assert!(!e.paused);
        e.authenticate(Some(Some("bob")));
        assert!(e.paused);
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
            gateway_url: "wss://ws-dev.thelightbulb.company".into(),
            relay_url: "wss://bv-dev.thelightbulb.company/api/v1/gateway/ws".into(),
            gateway_token: "unused".into(),
            display_name: "Mac".into(),
            paired_permissions: None,
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

    /// The hook subcommand must never emit a permission decision. Every path
    /// through it is silent today; this pins the guard that keeps it that way.
    ///
    /// The descriptor is this test's own, not the process's fd 1. Taking fd 1
    /// over is what the guard does in production, but cargo runs tests as
    /// threads and libtest's capture is per-thread, so any direct fd-1 write
    /// from another thread -- a `println!` off a tokio worker, say -- lands in
    /// this test's file and fails it. `silence` is the same `dup2` whatever the
    /// number, so the guard is proved on a descriptor nobody else can touch.
    #[test]
    fn local_sessions_hook_stdout_can_never_answer_a_permission_prompt() {
        use std::io::Write;
        use std::os::unix::io::{AsRawFd, FromRawFd};
        const ALLOW: &[u8] = br#"{"hookSpecificOutput":{"permissionDecision":"allow"}}"#;
        let dir = tempfile::tempdir().unwrap();
        let captured = dir.path().join("stdout");
        let file = fs::File::create(&captured).unwrap();
        // Stands in for a hook's stdout: whatever is written here would reach
        // Claude. `silence` closes this end by replacing what it points at, so
        // the `File` keeps owning the number and closes it on drop.
        let out = unsafe { libc::dup(file.as_raw_fd()) };
        assert!(out >= 0);
        let mut stdout = unsafe { fs::File::from_raw_fd(out) };
        stdout.write_all(b"reachable").unwrap();
        stdout.flush().unwrap();
        assert_eq!(
            fs::read(&captured).unwrap(),
            b"reachable",
            "the descriptor has to reach the reader, or the guard proves nothing"
        );

        silence(out);
        let _ = stdout.write_all(ALLOW);
        let _ = stdout.flush();
        assert_eq!(
            fs::read(&captured).unwrap(),
            b"reachable",
            "a hook that reaches stdout can allow or deny a tool call"
        );
        assert_eq!(
            HOOK_DECISION_FD,
            libc::STDOUT_FILENO,
            "and the hook silences its real stdout, which is what Claude reads"
        );
    }

    #[test]
    fn local_sessions_reassert_moves_both_providers_hooks_to_the_current_executable() {
        let state = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        fs::create_dir_all(home.path().join(".claude")).unwrap();
        fs::create_dir_all(home.path().join(".codex")).unwrap();
        let settings = home.path().join(".claude/settings.json");
        let record = state.path().join("hook-executable.json");
        let old = Path::new("/old/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let new = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        claude::configure_hooks(&settings, old, true).unwrap();
        codex::configure_hooks(home.path(), old, true).unwrap();
        private_write(&record, &old).unwrap();

        assert!(reassert_at(state.path(), home.path(), new).is_empty());

        let installed: Value = serde_json::from_slice(&fs::read(&settings).unwrap()).unwrap();
        let codex_hooks: Value =
            serde_json::from_slice(&fs::read(home.path().join(".codex/hooks.json")).unwrap())
                .unwrap();
        for (hooks, provider) in [(&installed, "claude"), (&codex_hooks, "codex")] {
            let entries = hooks["hooks"]["SessionStart"].as_array().unwrap();
            assert_eq!(entries.len(), 1, "{provider}: the dead path must be removed");
            assert_eq!(
                entries[0]["hooks"][0]["command"],
                format!("'{}' local-session-hook {provider}", new.display())
            );
        }
        let stale = lifecycle::codex_trust_hash(
            "stop",
            None,
            &format!("'{}' local-session-hook codex", old.display()),
            5,
            true,
            None,
        );
        let config = fs::read_to_string(home.path().join(".codex/config.toml")).unwrap();
        assert!(!config.contains(&stale), "the dead path stays trusted");
        assert_eq!(private_read::<PathBuf>(&record).unwrap(), new);
        // A resume installs the skill too: this is the only caller, so nothing
        // else would notice the call going missing.
        assert_eq!(
            fs::read_to_string(
                home.path()
                    .join(".claude/skills/lightbulb-session-sharing/SKILL.md")
            )
            .unwrap(),
            SKILL_MD
        );
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

    #[test]
    fn local_sessions_stale_relay_url_is_rewritten_to_the_pair_response() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("enrollment.json");
        let mut stale = candidate();
        stale.gateway_url = "wss://ws.flickmeon.ai".into();
        private_write(&path, &stale).unwrap();
        assert!(adopt_gateway_url(&path, "wss://ws.thelightbulb.company").unwrap());
        let read: Enrollment = private_read(&path).unwrap();
        assert_eq!(read.gateway_url, "wss://ws.thelightbulb.company");
        assert_eq!(read.server_revision, stale.server_revision);
        assert!(!adopt_gateway_url(&path, "wss://ws.thelightbulb.company").unwrap());
    }

    #[test]
    fn local_sessions_repair_success_replaces_a_prior_failure() {
        assert_eq!(retried(&["Codex hooks were not installed (x)".into()]), "Setup retried; Codex hooks were not installed (x).");
        assert_eq!(retried(&[]), "Setup retried; Claude Code, Codex, Cursor and Grok CLI are configured for new sessions.");
    }

    #[test]
    fn local_sessions_provider_status_reads_what_a_new_session_would_load() {
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let state = |p: &str, k: &str| provider_status(h, exe)[p][k].clone();
        for p in ["claude", "codex"] {
            assert_eq!((state(p, "mcp"), state(p, "hooks")), (json!("missing"), json!("missing")));
        }
        fs::create_dir_all(h.join(".claude")).unwrap();
        fs::create_dir_all(h.join(".codex")).unwrap();
        let settings = h.join(".claude/settings.json");
        claude::configure_hooks(&settings, exe, true).unwrap();
        codex::configure_hooks(h, exe, true).unwrap();
        assert_eq!(state("claude", "hooks"), "configured");
        assert_eq!(state("codex", "hooks"), "configured");
        let mut doc: Value = serde_json::from_slice(&fs::read(&settings).unwrap()).unwrap();
        doc["disableAllHooks"] = json!(true);
        fs::write(&settings, doc.to_string()).unwrap();
        assert_eq!(state("claude", "hooks"), "disabled");
        // An untrusted Codex group is skipped silently, so it is not configured.
        fs::write(h.join(".codex/config.toml"), "").unwrap();
        assert_eq!(state("codex", "hooks"), "missing");

        // The engine's `lightbulb` is the configured one; our old `session-mcp` entry is not.
        let server = |cmd: &str, arg: &str| json!({"mcpServers":{"lightbulb":{"command":cmd,"args":[arg]}}});
        fs::write(h.join(".claude.json"), server("/Users/x/.lightbulb/connect/bin/lightbulb", "mcp").to_string()).unwrap();
        assert_eq!(state("claude", "mcp"), "configured");
        fs::write(h.join(".claude.json"), server(exe.to_str().unwrap(), "session-mcp").to_string()).unwrap();
        assert_eq!(state("claude", "mcp"), "missing");
        let toml = |cmd: &str, arg: &str| format!("[mcp_servers.lightbulb]\ncommand = \"{cmd}\"\nargs = [\"{arg}\"]\n");
        fs::write(h.join(".codex/config.toml"), toml("/Users/x/.lightbulb/connect/bin/lightbulb", "mcp")).unwrap();
        assert_eq!(state("codex", "mcp"), "configured");
        fs::write(h.join(".codex/config.toml"), toml(exe.to_str().unwrap(), "session-mcp")).unwrap();
        assert_eq!(state("codex", "mcp"), "missing");
    }
}
