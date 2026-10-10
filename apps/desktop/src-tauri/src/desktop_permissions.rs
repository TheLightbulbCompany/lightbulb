//! Consent once, at pairing — never per operation.
//!
//! The person who pairs this Mac chooses what agents may do on it (shell, browser,
//! files, screen, Paperclip operations); that choice is the consent (owner ruling
//! 2026-09-26: "it all happens at the beginning when you get the app"). The backend
//! still asks this Mac to approve each shell command / Paperclip operation
//! (`system.run.approve.v1` / `desktop.operation.approve.v1`, same strict request shape
//! and expiry as before) and this module answers from the stored policy instead of
//! opening a window. Every action stays logged in the session / Activity as before.
//!
//! The store is `<app_data_dir>/desktop_permissions.json` (0600, atomic rename). A
//! missing or corrupt file allows nothing: the pairing dialog and Settings toggles write
//! it through `desktop_permissions_set`. Those are web UI, and any script on the page can
//! call the command too, so turning a switch ON needs a native "Allow" the page cannot
//! draw or click. That is still consent once: it asks when the set widens (at pairing or a
//! later Settings change), never during an agent operation. Turning things off asks nothing,
//! and a pairing can only narrow what this store already allows.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use tauri::Manager;

pub const FILE_NAME: &str = "desktop_permissions.json";

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct Store {
    /// The person paired this Mac and chose what agents may do on it.
    #[serde(default)]
    pub consented: bool,
    #[serde(default)]
    pub shell: bool,
    #[serde(default)]
    pub browser: bool,
    #[serde(default)]
    pub files: bool,
    #[serde(default)]
    pub screen: bool,
    /// Paperclip (board) runs may operate this Mac at all; each operation still needs
    /// its own capability above.
    #[serde(default)]
    pub paperclip: bool,
    #[serde(default)]
    pub updated_at: String,
}

fn path_in(app_data: &Path) -> PathBuf {
    app_data.join(FILE_NAME)
}

fn app_data(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_data_dir()
        .map_err(|e| format!("app_data_dir: {e}"))
}

pub fn load_from(app_data: &Path) -> Store {
    // Anything unreadable or malformed allows nothing.
    std::fs::read(path_in(app_data))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Store>(&bytes).ok())
        .unwrap_or_default()
}

pub fn save_to(app_data: &Path, store: &Store) -> Result<(), String> {
    std::fs::create_dir_all(app_data).map_err(|e| format!("create app data dir: {e}"))?;
    let path = path_in(app_data);
    let tmp = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(store).map_err(|e| e.to_string())?;
    std::fs::write(&tmp, bytes).map_err(|e| format!("write {}: {e}", tmp.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("chmod {}: {e}", tmp.display()))?;
    }
    std::fs::rename(&tmp, &path).map_err(|e| format!("rename {}: {e}", path.display()))
}

pub fn load(app: &tauri::AppHandle) -> Store {
    app_data(app).map(|d| load_from(&d)).unwrap_or_default()
}

fn now_iso() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    // Seconds since the epoch is enough for "when did this change"; the web shows it
    // through the platform's own formatter.
    secs.to_string()
}

/// The page's pairing switches, limited to what this Mac already allows. Turning one on
/// is `desktop_permissions_set`'s job (behind a native alert), never a pairing's.
pub fn within_consent(
    store: &Store,
    p: &crate::pair_client::DesktopPermissions,
) -> crate::pair_client::DesktopPermissions {
    let on = store.consented;
    crate::pair_client::DesktopPermissions {
        screen: on && store.screen && p.screen,
        shell: on && store.shell && p.shell,
        browser: on && store.browser && p.browser,
        files: on && store.files && p.files,
    }
}

/// A pairing narrows the store to what was paired; it never turns anything on and never
/// creates consent (the pairing dialog saved it through `desktop_permissions_set` first).
pub fn record_from_pairing(
    app_data: &Path,
    permissions: Option<&crate::pair_client::DesktopPermissions>,
) -> Result<(), String> {
    let previous = load_from(app_data);
    let Some(p) = permissions.filter(|_| previous.consented) else {
        return Ok(());
    };
    let p = within_consent(&previous, p);
    save_to(
        app_data,
        &Store {
            shell: p.shell,
            browser: p.browser,
            files: p.files,
            screen: p.screen,
            updated_at: now_iso(),
            ..previous
        },
    )
}

/// The capabilities `next` turns on that `previous` did not allow, in plain words for the
/// native alert. A store without consent allows nothing, so all of `next` counts as new.
pub fn newly_enabled(previous: &Store, next: &Store) -> Vec<&'static str> {
    let was = |on: bool| previous.consented && on;
    [
        (next.shell && !was(previous.shell), "Run shell commands"),
        (next.browser && !was(previous.browser), "Use a browser"),
        (next.files && !was(previous.files), "Read and write files in approved folders"),
        (next.screen && !was(previous.screen), "See your screen"),
        (next.paperclip && !was(previous.paperclip), "Let Paperclip runs operate this Mac"),
    ]
    .into_iter()
    .filter_map(|(new, label)| new.then_some(label))
    .collect()
}

/// Whether the alert the person allowed (`shown`) lists everything `next` would turn on
/// over the store as it is now.
pub fn alert_covers(shown: &[&str], now: &Store, next: &Store) -> bool {
    newly_enabled(now, next).iter().all(|c| shown.contains(c))
}

static SET_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[tauri::command]
pub fn desktop_permissions_get(app: tauri::AppHandle) -> Result<Store, String> {
    Ok(load(&app))
}

/// Saving IS the consent. The webview passes the whole set; `consented` and the
/// timestamp are set here, never trusted from IPC. Anything turned on needs a native
/// "Allow" first; Cancel saves nothing. Browser switched off also ends the agent
/// Chrome's CDP channels and deletes its profile's sign-ins.
#[tauri::command]
pub async fn desktop_permissions_set(app: tauri::AppHandle, permissions: Store) -> Result<Store, String> {
    let store = Store {
        consented: true,
        updated_at: now_iso(),
        ..permissions
    };
    let dir = app_data(&app)?;
    // One save at a time, alert included: a call waiting on the alert must not write its
    // older set over a revocation another call saved meanwhile.
    let _serialized = SET_LOCK.lock().await;
    let previous = load_from(&dir);
    let added = newly_enabled(&previous, &store);
    if !added.is_empty()
        && !crate::confirm_native(
            &app,
            "Allow agents on this Mac?",
            format!(
                "Lightbulb wants to let agents in your workspace:\n\n{}\n\nOnly allow this if you just chose it in Lightbulb.",
                added.join("\n")
            ),
        )
        .await
    {
        return Err(crate::DECLINED_ON_MAC.into());
    }
    // A pairing may still have narrowed the store while the alert was open; saving would
    // turn back on something the alert never listed.
    if !alert_covers(&added, &load_from(&dir), &store) {
        return Err(crate::DECLINED_ON_MAC.into());
    }
    save_to(&dir, &store)?;
    if store.screen && !previous.screen {
        crate::screen::request_access(&dir);
    }
    if previous.browser && !store.browser {
        crate::revoke_agent_browser(&app).await;
    }
    crate::log(&format!(
        "[permissions] saved shell={} browser={} files={} screen={} paperclip={}",
        store.shell, store.browser, store.files, store.screen, store.paperclip
    ));
    Ok(store)
}

// ---- approval answers ---------------------------------------------------------------

/// The backend's request shape, unchanged from the approve-once window: strict fields,
/// a 64-hex binding echoed back, ≤120 s expiry, and exactly one of {shell argv, typed
/// Paperclip operation}.
#[derive(Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Approval {
    binding: String,
    workspace: String,
    agent: String,
    device: String,
    run: String,
    argv: Option<Vec<String>>,
    cwd: Option<String>,
    env: Option<std::collections::BTreeMap<String, String>>,
    company: Option<String>,
    native_device: Option<String>,
    operation: Option<Value>,
    expires_at_ms: u64,
}

enum Kind<'a> {
    Shell,
    Operation(&'a str),
}

impl Approval {
    fn kind(&self) -> Option<Kind<'_>> {
        match (
            &self.operation,
            &self.company,
            &self.native_device,
            &self.argv,
            &self.cwd,
            &self.env,
        ) {
            (Some(operation), Some(company), Some(native), None, None, None) => {
                let command = operation.get("command").and_then(Value::as_str)?;
                let ok = !company.is_empty()
                    && company.len() <= 128
                    && !native.is_empty()
                    && native.len() <= 128
                    && operation.is_object()
                    && operation.to_string().len() <= 32768
                    && !command.is_empty()
                    && command.len() <= 128
                    && operation.get("params").is_some_and(Value::is_object);
                ok.then_some(Kind::Operation(command))
            }
            (None, None, None, Some(argv), Some(cwd), Some(env)) => {
                let ok = !argv.is_empty()
                    && argv.len() <= 64
                    && !argv.iter().any(|s| s.len() > 4096 || s.contains('\0'))
                    && cwd.starts_with('/')
                    && cwd.len() <= 4096
                    && env.len() <= 16;
                ok.then_some(Kind::Shell)
            }
            _ => None,
        }
    }
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Which consent an operation needs, by its command family. Commands outside these
/// families (device probes, notifications) need only the Paperclip consent itself.
fn allows_operation(store: &Store, command: &str) -> bool {
    let cap = if command == "system.run" || command.starts_with("system.run.") {
        store.shell
    } else if command.starts_with("browser.") {
        store.browser
    } else if command.starts_with("file.") || command.starts_with("dir.") {
        store.files
    } else if command.starts_with("screen.") {
        store.screen
    } else {
        true
    };
    store.paperclip && cap
}

/// Pure decision: the stored policy against one request. `{approved, binding}` is what
/// the backend compares byte for byte, so the binding is echoed on every path that
/// parsed one.
pub fn decide_with(store: &Store, params: Value) -> Value {
    let Ok(p) = serde_json::from_value::<Approval>(params) else {
        return json!({"approved": false});
    };
    let remaining = p.expires_at_ms.saturating_sub(now_ms());
    let well_formed = remaining > 0
        && remaining <= 120_000
        && p.binding.len() == 64
        && p.binding.bytes().all(|b| b.is_ascii_hexdigit())
        && [&p.workspace, &p.agent, &p.device, &p.run]
            .iter()
            .all(|s| !s.is_empty() && s.len() <= 128);
    let approved = well_formed
        && store.consented
        && match p.kind() {
            Some(Kind::Shell) => store.shell,
            Some(Kind::Operation(command)) => allows_operation(store, command),
            None => false,
        };
    json!({"approved": approved, "binding": p.binding})
}

pub fn decide(app: &tauri::AppHandle, params: Value) -> Value {
    let store = load(app);
    let answer = decide_with(&store, params);
    crate::log(&format!(
        "[permissions] approval answered approved={}",
        answer["approved"]
    ));
    answer
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all_on() -> Store {
        Store {
            consented: true,
            shell: true,
            browser: true,
            files: true,
            screen: true,
            paperclip: true,
            updated_at: "1".into(),
        }
    }

    fn shell_request() -> Value {
        json!({"binding":"a".repeat(64),"workspace":"org_1","agent":"agent_1","device":"cli",
            "run":"run_1","argv":["echo","hi"],"cwd":"/tmp","env":{},"expiresAtMs":now_ms()+60_000})
    }

    fn operation_request(command: &str) -> Value {
        json!({"binding":"b".repeat(64),"workspace":"org_1","company":"company_1",
            "agent":"agent_1","run":"run_1","device":"cli","nativeDevice":"native",
            "operation":{"command":command,"params":{}},"expiresAtMs":now_ms()+60_000})
    }

    #[test]
    fn nothing_is_allowed_before_consent_or_after_a_revoke() {
        let no_consent = Store { consented: false, ..all_on() };
        assert_eq!(decide_with(&no_consent, shell_request())["approved"], false);
        let revoked = Store { shell: false, ..all_on() };
        let answer = decide_with(&revoked, shell_request());
        assert_eq!(answer["approved"], false);
        assert_eq!(answer["binding"], "a".repeat(64));
        assert_eq!(decide_with(&Store::default(), operation_request("device.info"))["approved"], false);
    }

    #[test]
    fn a_granted_capability_answers_approved_and_echoes_the_binding() {
        let answer = decide_with(&all_on(), shell_request());
        assert_eq!(answer, json!({"approved": true, "binding": "a".repeat(64)}));
        let answer = decide_with(&all_on(), operation_request("browser.proxy"));
        assert_eq!(answer, json!({"approved": true, "binding": "b".repeat(64)}));
    }

    #[test]
    fn a_paperclip_operation_needs_paperclip_and_its_own_capability() {
        let no_paperclip = Store { paperclip: false, ..all_on() };
        assert_eq!(decide_with(&no_paperclip, operation_request("device.info"))["approved"], false);
        let no_browser = Store { browser: false, ..all_on() };
        assert_eq!(decide_with(&no_browser, operation_request("browser.proxy"))["approved"], false);
        assert_eq!(decide_with(&no_browser, operation_request("system.run"))["approved"], true);
        // A Paperclip run's Mac browser acquire (browser.cdp.v1) follows the Browser switch.
        assert_eq!(decide_with(&no_browser, operation_request("browser.cdp.v1"))["approved"], false);
        assert_eq!(decide_with(&all_on(), operation_request("browser.cdp.v1"))["approved"], true);
        let no_files = Store { files: false, ..all_on() };
        assert_eq!(decide_with(&no_files, operation_request("file.fetch"))["approved"], false);
        assert_eq!(decide_with(&no_files, operation_request("dir.list"))["approved"], false);
        let no_screen = Store { screen: false, ..all_on() };
        assert_eq!(decide_with(&no_screen, operation_request("screen.snapshot"))["approved"], false);
        assert_eq!(decide_with(&no_screen, operation_request("device.info"))["approved"], true);
    }

    #[test]
    fn malformed_mixed_or_expired_requests_are_refused_even_with_full_consent() {
        let mut mixed = operation_request("device.info");
        mixed["argv"] = json!(["echo"]);
        assert_eq!(decide_with(&all_on(), mixed)["approved"], false);
        let mut typed_wrong = operation_request("device.info");
        typed_wrong["operation"] = json!("not a typed operation");
        assert_eq!(decide_with(&all_on(), typed_wrong)["approved"], false);
        let mut expired = shell_request();
        expired["expiresAtMs"] = json!(now_ms() - 1);
        assert_eq!(decide_with(&all_on(), expired)["approved"], false);
        let mut too_long = shell_request();
        too_long["expiresAtMs"] = json!(now_ms() + 600_000);
        assert_eq!(decide_with(&all_on(), too_long)["approved"], false);
        let mut bad_binding = shell_request();
        bad_binding["binding"] = json!("xyz");
        assert_eq!(decide_with(&all_on(), bad_binding)["approved"], false);
        let mut unknown_field = shell_request();
        unknown_field["approved"] = json!(true);
        assert_eq!(decide_with(&all_on(), unknown_field), json!({"approved": false}));
    }

    #[test]
    fn the_store_persists_and_a_missing_or_corrupt_file_allows_nothing() {
        let dir = std::env::temp_dir().join(format!("lb-perm-{}", uuid::Uuid::new_v4()));
        assert_eq!(load_from(&dir), Store::default());
        save_to(&dir, &all_on()).unwrap();
        assert_eq!(load_from(&dir), all_on());
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mode = std::fs::metadata(path_in(&dir)).unwrap().permissions().mode() & 0o777;
            assert_eq!(mode, 0o600);
        }
        std::fs::write(path_in(&dir), b"{not json").unwrap();
        assert_eq!(load_from(&dir), Store::default());
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_pairing_only_narrows_and_never_creates_consent() {
        let dir = std::env::temp_dir().join(format!("lb-perm-{}", uuid::Uuid::new_v4()));
        let all = crate::pair_client::DesktopPermissions { screen: true, shell: true, browser: true, files: true };
        record_from_pairing(&dir, Some(&all)).unwrap();
        assert_eq!(load_from(&dir), Store::default(), "a pairing without saved consent grants nothing");
        save_to(&dir, &Store { shell: false, paperclip: false, ..all_on() }).unwrap();
        record_from_pairing(&dir, Some(&all)).unwrap();
        let after = load_from(&dir);
        assert!(!after.shell && !after.paperclip, "a pairing must not turn a switch back on");
        let fewer = crate::pair_client::DesktopPermissions { files: false, ..all };
        record_from_pairing(&dir, Some(&fewer)).unwrap();
        assert!(!load_from(&dir).files && load_from(&dir).browser, "a pairing narrows");
        record_from_pairing(&dir, None).unwrap();
        assert!(load_from(&dir).consented, "no permissions in the pair request leaves the store alone");
        assert!(!within_consent(&Store::default(), &all).shell);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn only_switches_turned_on_need_the_native_alert() {
        let off = Store { consented: true, ..Store::default() };
        let first = Store { shell: true, files: true, ..off.clone() };
        assert_eq!(
            newly_enabled(&Store::default(), &first),
            ["Run shell commands", "Read and write files in approved folders"]
        );
        assert!(newly_enabled(&first, &Store { shell: false, ..first.clone() }).is_empty());
        assert!(newly_enabled(&first, &first).is_empty());
        assert_eq!(newly_enabled(&first, &Store { screen: true, ..first.clone() }), ["See your screen"]);
        // A store without consent allows nothing, whatever its switches say.
        let stale = Store { consented: false, ..all_on() };
        assert_eq!(newly_enabled(&stale, &all_on()).len(), 5);
    }

    #[test]
    fn a_save_refuses_what_changed_after_the_alert() {
        let shell_on = Store { consented: true, shell: true, ..Store::default() };
        let next = Store { files: true, ..shell_on.clone() };
        let shown = newly_enabled(&shell_on, &next);
        assert!(alert_covers(&shown, &shell_on, &next));
        // Shell was turned off while the alert was open: saving `next` would turn it back on.
        let revoked = Store { shell: false, ..shell_on };
        assert!(!alert_covers(&shown, &revoked, &next));
    }
}
