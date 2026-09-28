//! `browser.state.v1`: the agent profile's workspace sign-ins (cookies + localStorage)
//! exported or imported in ONE local operation, over the `lightbulb` profile's own CDP port
//! with the bundled agent-browser -- the tool the cloud browser's
//! `apps/infra/hermes/bridge/browser_state.py` uses.
//!
//! The backend (`core/services/desktop_browser_state.py`) keeps every merge, fence and
//! ownership decision, and presigns one S3 object per direction under its backend-only
//! `browser-state/<owner>/` prefix. This module only moves that file between S3 and Chrome.
//! Before it, each cookie batch and each origin's localStorage was its own `browser.proxy`
//! round trip through the backend relay (~1-2 s each), so a second run's sync could not fit
//! one page action's deadline.
//!
//! What it keeps from the per-call path it replaces:
//! - only the `lightbulb` profile, never the everyday `chrome` one, and only with Browser on
//!   (the dispatch hook checks `desktop_permissions`);
//! - localStorage is read or replaced only by a script that checks `location.origin` in the
//!   same turn, in a tab of its own, so a redirect never touches another site's storage;
//! - an import clears the jar first (an empty save still signs everything out) and replaces
//!   each listed origin's localStorage (an empty list clears it);
//! - no cookie or storage value reaches a log line, an error or the node result: values only
//!   pass through a private 0700 scratch directory (deleted afterwards), a script on the
//!   CLI's stdin, and the presigned URL.

use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

const PROFILE: &str = "lightbulb";
const SESSION: &str = "lb";
const MAX_STATE_BYTES: usize = 50 * 1024 * 1024;
const MAX_ORIGINS: usize = 500;
/// One agent-browser command. A page that does not load in its default timeout fails alone.
const STEP: Duration = Duration::from_secs(15);
const PAGE_TIMEOUT_MS: &str = "10000";
/// Kept back from the node deadline for the S3 transfer and cleanup after the origins.
const RESERVE: Duration = Duration::from_secs(4);

pub type Failure = (&'static str, String);

fn failed() -> Failure {
    (
        "BROWSER_STATE_FAILED",
        "The workspace sign-in sync on this Mac's agent browser failed.".into(),
    )
}

fn invalid(why: &str) -> Failure {
    ("INVALID_PARAMS", why.into())
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
struct Params {
    action: String,
    profile: String,
    url: String,
    #[serde(default)]
    base_url: Option<String>,
    #[serde(default)]
    origins: Vec<String>,
}

#[derive(Debug, PartialEq)]
pub enum Action {
    /// PUT the profile's cookies plus localStorage for its open tabs' origins and `origins`.
    Export { url: String, origins: Vec<String> },
    /// GET a state, make the profile hold it, then PUT the profile's own read-back to `base_url`.
    Import {
        url: String,
        base_url: Option<String>,
    },
}

pub fn parse(params: Value) -> Result<Action, Failure> {
    let p: Params =
        serde_json::from_value(params).map_err(|_| invalid("invalid browser.state.v1 params"))?;
    if p.profile != PROFILE {
        return Err((
            "PROFILE_REFUSED",
            "Only the lightbulb agent profile syncs workspace sign-ins.".into(),
        ));
    }
    check_url(&p.url)?;
    match p.action.as_str() {
        "export" if p.base_url.is_none() => {
            if p.origins.len() > MAX_ORIGINS {
                return Err(invalid("too many origins"));
            }
            // An origin this Mac would name differently is simply not read: it keeps the
            // workspace's copy, never fails the export.
            let origins = p
                .origins
                .into_iter()
                .filter(|o| origin(o).as_deref() == Some(o.as_str()))
                .collect();
            Ok(Action::Export {
                url: p.url,
                origins,
            })
        }
        "import" if p.origins.is_empty() => {
            if let Some(base) = &p.base_url {
                check_url(base)?;
            }
            Ok(Action::Import {
                url: p.url,
                base_url: p.base_url,
            })
        }
        _ => Err(invalid("action must be export or import")),
    }
}

/// The rules of `bridge/browser_state.py::_valid_url`: HTTPS on 443 to an S3 endpoint, no
/// credentials, no fragment. (No redirects either: see [`http`].)
pub fn check_url(value: &str) -> Result<(), Failure> {
    let bad = || invalid("state URL must be a presigned HTTPS S3 URL");
    let url = url::Url::parse(value).map_err(|_| bad())?;
    let Some(url::Host::Domain(host)) = url.host() else {
        return Err(bad());
    };
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.port().is_some_and(|port| port != 443)
    {
        return Err(bad());
    }
    if !(host.ends_with(".amazonaws.com")
        && (host.starts_with("s3.") || host.contains(".s3.") || host.contains(".s3-")))
    {
        return Err(bad());
    }
    Ok(())
}

/// `scheme://host[:port]` of an http(s) URL, as the page's `location.origin` reports it.
pub fn origin(value: &str) -> Option<String> {
    let url = url::Url::parse(value).ok()?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return None;
    }
    Some(url.origin().ascii_serialization())
}

/// The agent profile's CDP port from the node host's `openclaw.json`
/// (`browser.profiles.lightbulb.cdpPort`, 18810 as `local_mcp` configures it).
pub fn cdp_port(config: &Value) -> Result<u16, Failure> {
    let unconfigured = || {
        (
            "BROWSER_NOT_CONFIGURED",
            "This Mac's agent browser profile is not configured.".to_string(),
        )
    };
    let profile = config
        .pointer("/browser/profiles/lightbulb")
        .ok_or_else(unconfigured)?;
    if profile
        .get("driver")
        .and_then(Value::as_str)
        .unwrap_or("openclaw")
        != "openclaw"
    {
        return Err(unconfigured());
    }
    profile
        .get("cdpPort")
        .and_then(Value::as_u64)
        .filter(|port| (1024..=65535).contains(port))
        .map(|port| port as u16)
        .ok_or_else(unconfigured)
}

/// Every agent-browser call: JSON output, one private session, attached to the profile's CDP
/// port (it disconnects on `close`, never quitting a browser it did not launch).
pub fn command_args(port: u16, args: &[&str]) -> Vec<String> {
    let mut all: Vec<String> = ["--json", "--session", SESSION, "--cdp"]
        .iter()
        .map(|a| a.to_string())
        .collect();
    all.push(port.to_string());
    all.extend(args.iter().map(|a| a.to_string()));
    all
}

const STORAGE_JS: &str = "const back = []; for (let i = 0; i < localStorage.length; i++) { \
     const name = localStorage.key(i); back.push({ name, value: localStorage.getItem(name) }); } return back;";

/// The page's localStorage, or null when the page is not on `origin` (it redirected).
pub fn read_script(origin: &str) -> String {
    let want = serde_json::to_string(origin).unwrap_or_default();
    format!("(() => {{ if (location.origin !== {want}) return null; {STORAGE_JS} }})()")
}

/// Replace the page's localStorage with `items`, in the same turn as the origin check; return
/// what it holds afterwards, or null when the page is not on `origin`.
pub fn replace_script(origin: &str, items: &[Value]) -> String {
    let want = serde_json::to_string(origin).unwrap_or_default();
    let items = serde_json::to_string(items).unwrap_or_else(|_| "[]".into());
    format!(
        "(() => {{ if (location.origin !== {want}) return null; localStorage.clear(); \
         for (const item of {items}) localStorage.setItem(item.name, item.value); {STORAGE_JS} }})()"
    )
}

/// `{name, value}` string pairs only.
pub fn storage_items(value: &Value) -> Option<Vec<Value>> {
    let items = value.as_array()?;
    Some(
        items
            .iter()
            .filter(|i| {
                i.get("name").is_some_and(Value::is_string)
                    && i.get("value").is_some_and(Value::is_string)
            })
            .map(|i| json!({"name": i["name"], "value": i["value"]}))
            .collect(),
    )
}

/// Origins of every http(s) `url` in agent-browser's `tab list` data.
pub fn tab_origins(data: &Value, into: &mut BTreeSet<String>) {
    match data {
        Value::Object(map) => {
            for (key, item) in map {
                match (key.as_str(), item) {
                    ("url", Value::String(url)) => into.extend(origin(url)),
                    _ => tab_origins(item, into),
                }
            }
        }
        Value::Array(items) => items.iter().for_each(|item| tab_origins(item, into)),
        _ => {}
    }
}

fn valid_state(body: &[u8]) -> Result<Value, Failure> {
    let state: Value = serde_json::from_slice(body).map_err(|_| failed())?;
    if !state.get("cookies").is_some_and(Value::is_array)
        || !state.get("origins").is_some_and(Value::is_array)
    {
        return Err(failed());
    }
    Ok(state)
}

/// A private, short-named scratch directory (agent-browser's daemon socket lives in it, and a
/// unix socket path is capped near 104 bytes), removed with everything in it on drop.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> std::io::Result<Self> {
        use std::os::unix::fs::DirBuilderExt;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let dir = PathBuf::from("/tmp").join(format!("lb-{}", &id[..12]));
        std::fs::DirBuilder::new().mode(0o700).create(&dir)?;
        Ok(Self(dir))
    }

    fn write(&self, name: &str, body: &[u8]) -> Result<PathBuf, Failure> {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let path = self.0.join(name);
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
            .and_then(|mut file| file.write_all(body))
            .map_err(|_| failed())?;
        Ok(path)
    }

    fn read(path: &Path) -> Result<Vec<u8>, Failure> {
        let len = std::fs::symlink_metadata(path).map_err(|_| failed())?.len();
        if len as usize > MAX_STATE_BYTES {
            return Err(failed());
        }
        std::fs::read(path).map_err(|_| failed())
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// `bin/agent-browser-<triple>`, a bundle RESOURCE beside the node host (never Contents/MacOS:
/// see `lib.rs::resolve_node_host_binary`).
fn binary() -> Result<PathBuf, Failure> {
    const BIN: &str = if cfg!(target_arch = "aarch64") {
        "bin/agent-browser-aarch64-apple-darwin"
    } else {
        "bin/agent-browser-x86_64-apple-darwin"
    };
    let exe = std::env::current_exe().map_err(|_| failed())?;
    let parent = exe.parent().ok_or_else(failed)?;
    [parent.join("../Resources").join(BIN), parent.join(BIN)]
        .into_iter()
        .find(|path| path.is_file())
        .ok_or((
            "BROWSER_STATE_UNAVAILABLE",
            "This desktop build has no agent-browser.".into(),
        ))
}

struct Browser {
    bin: PathBuf,
    port: u16,
    scratch: Scratch,
    namespace: String,
}

impl Browser {
    /// One agent-browser command's `data`, or Err. Never logs its output.
    async fn call(&self, args: &[&str], stdin: Option<&[u8]>) -> Result<Value, ()> {
        let mut command = Command::new(&self.bin);
        command
            .args(command_args(self.port, args))
            .env_clear()
            .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            // Never the person's own ~/.agent-browser config, saved sessions or keys.
            .env("HOME", &self.scratch.0)
            .env("TMPDIR", &self.scratch.0)
            .env("AGENT_BROWSER_SOCKET_DIR", &self.scratch.0)
            .env("AGENT_BROWSER_NAMESPACE", &self.namespace)
            .env("AGENT_BROWSER_DEFAULT_TIMEOUT", PAGE_TIMEOUT_MS)
            .env("AGENT_BROWSER_NO_WEBMCP", "1")
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let run = async {
            let mut child = command.spawn().map_err(|_| ())?;
            if let (Some(bytes), Some(mut pipe)) = (stdin, child.stdin.take()) {
                pipe.write_all(bytes).await.map_err(|_| ())?;
            }
            child.wait_with_output().await.map_err(|_| ())
        };
        let out = tokio::time::timeout(STEP, run).await.map_err(|_| ())??;
        if !out.status.success() || out.stdout.len() > MAX_STATE_BYTES {
            return Err(());
        }
        let reply: Value = serde_json::from_slice(&out.stdout).map_err(|_| ())?;
        if reply.get("success") != Some(&Value::Bool(true)) {
            return Err(());
        }
        Ok(reply.get("data").cloned().unwrap_or(Value::Null))
    }

    /// The profile's cookie jar, via the same `state save` the cloud's files come from.
    async fn cookies(&self, name: &str) -> Result<Value, Failure> {
        let path = self.scratch.0.join(name);
        self.call(&["state", "save", &path.to_string_lossy()], None)
            .await
            .map_err(|_| failed())?;
        Ok(valid_state(&Scratch::read(&path)?)?["cookies"].clone())
    }

    /// Run `script` in a tab of its own opened on `origin`, then close that tab. Stops at
    /// `deadline`; `None` for an origin not done (unreachable, redirected, or out of time).
    async fn on_origin(
        &self,
        index: usize,
        origin: &str,
        script: &str,
        deadline: Instant,
    ) -> Option<Vec<Value>> {
        let label = format!("lb{index}");
        let left = deadline.saturating_duration_since(Instant::now());
        if left < Duration::from_millis(500) {
            return None;
        }
        let work = async {
            self.call(&["tab", "new", "--label", &label, origin], None)
                .await
                .ok()?;
            self.call(&["eval", "--stdin"], Some(script.as_bytes()))
                .await
                .ok()
        };
        let result = tokio::time::timeout(left, work).await.ok().flatten();
        let _ = self.call(&["tab", "close", &label], None).await;
        storage_items(result?.get("result")?)
    }
}

async fn download(http: &reqwest::Client, url: &str) -> Result<Vec<u8>, Failure> {
    let mut response = http.get(url).send().await.map_err(|_| failed())?;
    if !response.status().is_success() {
        return Err(failed());
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| failed())? {
        if body.len() + chunk.len() > MAX_STATE_BYTES {
            return Err(failed());
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

async fn upload(http: &reqwest::Client, url: &str, body: Vec<u8>) -> Result<(), Failure> {
    if body.len() > MAX_STATE_BYTES {
        return Err(failed());
    }
    let response = http
        .put(url)
        // Signed into the presigned URL by the backend (`browser_control._state_url`).
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(body)
        .send()
        .await
        .map_err(|_| failed())?;
    response
        .status()
        .is_success()
        .then_some(())
        .ok_or_else(failed)
}

/// No redirects: a presigned URL answering with one is refused, never followed.
fn http() -> Result<reqwest::Client, Failure> {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(60))
        .build()
        .map_err(|_| failed())
}

async fn export(
    b: &Browser,
    http: &reqwest::Client,
    url: &str,
    wanted: &[String],
    deadline: Instant,
) -> Result<(), Failure> {
    let mut origins: BTreeSet<String> = wanted.iter().cloned().collect();
    if let Ok(data) = b.call(&["tab", "list"], None).await {
        tab_origins(&data, &mut origins);
    }
    let cookies = b.cookies("export.json").await?;
    let mut read = Vec::new();
    for (index, origin) in origins.iter().take(MAX_ORIGINS).enumerate() {
        // Listed = read (an empty list when it holds nothing); absent = not read, which keeps
        // the workspace's copy on the backend.
        if let Some(items) = b
            .on_origin(index, origin, &read_script(origin), deadline)
            .await
        {
            read.push(json!({"origin": origin, "localStorage": items}));
        }
    }
    let body =
        serde_json::to_vec(&json!({"cookies": cookies, "origins": read})).map_err(|_| failed())?;
    upload(http, url, body).await
}

async fn import(
    b: &Browser,
    http: &reqwest::Client,
    url: &str,
    base_url: Option<&str>,
    deadline: Instant,
) -> Result<(), Failure> {
    let state = valid_state(&download(http, url).await?)?;
    // agent-browser's `state load` only ever adds cookies: clear first, so an empty saved jar
    // still replaces, never inherits, the profile's.
    b.call(&["cookies", "clear"], None)
        .await
        .map_err(|_| failed())?;
    if state["cookies"].as_array().is_some_and(|c| !c.is_empty()) {
        // Cookies only: `state load` would navigate the active tab to set localStorage without
        // checking where it landed, so storage is replaced per origin below instead.
        let body = serde_json::to_vec(&json!({"cookies": state["cookies"], "origins": []}))
            .map_err(|_| failed())?;
        let path = b.scratch.write("cookies.json", &body)?;
        b.call(&["state", "load", &path.to_string_lossy()], None)
            .await
            .map_err(|_| failed())?;
    }
    let mut reached = Vec::new();
    let listed = state["origins"].as_array().cloned().unwrap_or_default();
    for (index, entry) in listed.iter().take(MAX_ORIGINS).enumerate() {
        let Some(name) = entry
            .get("origin")
            .and_then(Value::as_str)
            .filter(|o| origin(o).as_deref() == Some(*o))
        else {
            continue;
        };
        let items = entry
            .get("localStorage")
            .and_then(storage_items)
            .unwrap_or_default();
        if let Some(back) = b
            .on_origin(index, name, &replace_script(name, &items), deadline)
            .await
        {
            reached.push(json!({"origin": name, "localStorage": back}));
        }
    }
    if let Some(base) = base_url {
        // The merge base is the profile's own read-back, so Chrome's normalisation is in it.
        let cookies = b.cookies("base.json").await?;
        let body = serde_json::to_vec(&json!({"cookies": cookies, "origins": reached}))
            .map_err(|_| failed())?;
        upload(http, base, body).await?;
    }
    Ok(())
}

/// The node command. `timeout_ms` is the invoke's own deadline; per-origin work stops short
/// of it so the file still reaches S3.
pub async fn run(
    app: &tauri::AppHandle,
    params_json: Option<&str>,
    timeout_ms: u64,
) -> Result<Value, Failure> {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms).saturating_sub(RESERVE);
    let params = params_json
        .and_then(|raw| serde_json::from_str(raw).ok())
        .unwrap_or(Value::Null);
    let action = parse(params)?;
    let state_dir = crate::openclaw_state_dir(app).map_err(|_| failed())?;
    let config: Value = std::fs::read(crate::local_mcp::config_path(&state_dir))
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .unwrap_or(Value::Null);
    let port = cdp_port(&config)?;
    let browser = Browser {
        bin: binary()?,
        port,
        scratch: Scratch::new().map_err(|_| failed())?,
        namespace: format!("lb-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]),
    };
    let http = http()?;
    let (kind, result) = match &action {
        Action::Export { url, origins } => (
            "export",
            export(&browser, &http, url, origins, deadline).await,
        ),
        Action::Import { url, base_url } => (
            "import",
            import(&browser, &http, url, base_url.as_deref(), deadline).await,
        ),
    };
    let _ = browser.call(&["close"], None).await;
    if let Err((code, _)) = &result {
        crate::log(&format!("[browser-state] {kind} failed: {code}"));
    }
    result.map(|()| json!({"ok": true}))
}

#[cfg(test)]
mod tests {
    use super::*;

    const S3: &str = "https://lightbulb-dev-openclaw-replicas.s3.amazonaws.com/browser-state/org_1/mac-export-ab.json?X-Amz-Signature=x";

    fn params(extra: Value) -> Value {
        let mut base = json!({"action": "export", "profile": "lightbulb", "url": S3});
        base.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        base
    }

    #[test]
    fn only_presigned_https_s3_urls_are_accepted() {
        for ok in [
            S3,
            "https://bucket.s3.us-east-1.amazonaws.com/k?sig",
            "https://s3.us-east-1.amazonaws.com/bucket/k",
            "https://bucket.s3-accelerate.amazonaws.com/k",
            "https://bucket.s3.amazonaws.com:443/k",
        ] {
            assert!(check_url(ok).is_ok(), "{ok}");
        }
        for bad in [
            "http://bucket.s3.amazonaws.com/k",
            "https://bucket.s3.amazonaws.com:8443/k",
            "https://user:pw@bucket.s3.amazonaws.com/k",
            "https://bucket.s3.amazonaws.com/k#frag",
            "https://s3.amazonaws.com.evil.test/k",
            "https://evil.test/s3.amazonaws.com",
            "https://example.amazonaws.com/k",
            "https://52.216.0.1/k",
            "file:///etc/passwd",
            "not a url",
        ] {
            assert!(check_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn refuses_any_profile_but_the_agent_one_and_unknown_fields() {
        assert_eq!(
            parse(params(json!({"profile": "chrome"}))).unwrap_err().0,
            "PROFILE_REFUSED"
        );
        assert_eq!(
            parse(params(json!({"profile": "user"}))).unwrap_err().0,
            "PROFILE_REFUSED"
        );
        assert_eq!(
            parse(params(json!({"cdpUrl": "ws://127.0.0.1:9222"})))
                .unwrap_err()
                .0,
            "INVALID_PARAMS"
        );
        assert_eq!(
            parse(params(json!({"action": "wipe"}))).unwrap_err().0,
            "INVALID_PARAMS"
        );
        assert_eq!(
            parse(params(json!({"baseUrl": S3}))).unwrap_err().0,
            "INVALID_PARAMS",
            "export takes no base"
        );
        let import = params(json!({"action": "import", "origins": ["https://a.test"]}));
        assert_eq!(
            parse(import).unwrap_err().0,
            "INVALID_PARAMS",
            "import clears only what its file lists"
        );
        let import = params(json!({"action": "import", "baseUrl": "https://evil.test/base"}));
        assert_eq!(parse(import).unwrap_err().0, "INVALID_PARAMS");
        assert_eq!(
            parse(params(json!({"url": "https://evil.test/x"})))
                .unwrap_err()
                .0,
            "INVALID_PARAMS"
        );
    }

    #[test]
    fn parses_both_actions_and_drops_origins_it_would_name_differently() {
        let export = parse(params(
            json!({"origins": ["https://a.test", "https://b.test/", "javascript:alert(1)", "http://c.test:8080"]}),
        ));
        assert_eq!(
            export.unwrap(),
            Action::Export {
                url: S3.into(),
                origins: vec!["https://a.test".into(), "http://c.test:8080".into()]
            }
        );
        let import = parse(params(json!({"action": "import", "baseUrl": S3}))).unwrap();
        assert_eq!(
            import,
            Action::Import {
                url: S3.into(),
                base_url: Some(S3.into())
            }
        );
    }

    #[test]
    fn reads_the_agent_profiles_cdp_port_only() {
        let config = json!({"browser": {"profiles": {
            "lightbulb": {"driver": "openclaw", "cdpPort": 18810},
            "chrome": {"driver": "extension", "cdpPort": 18811}
        }}});
        assert_eq!(cdp_port(&config).unwrap(), 18810);
        assert_eq!(
            cdp_port(&json!({"browser": {"profiles": {"lightbulb": {"cdpPort": 18810}}}})).unwrap(),
            18810
        );
        for bad in [
            json!({}),
            json!({"browser": {"profiles": {"chrome": {"driver": "extension", "cdpPort": 18811}}}}),
            json!({"browser": {"profiles": {"lightbulb": {"driver": "extension", "cdpPort": 18810}}}}),
            json!({"browser": {"profiles": {"lightbulb": {"driver": "openclaw", "cdpPort": 80}}}}),
            json!({"browser": {"profiles": {"lightbulb": {"driver": "openclaw"}}}}),
        ] {
            assert_eq!(
                cdp_port(&bad).unwrap_err().0,
                "BROWSER_NOT_CONFIGURED",
                "{bad}"
            );
        }
    }

    #[test]
    fn builds_one_private_cdp_session_per_call() {
        assert_eq!(
            command_args(18810, &["state", "save", "/tmp/lb-1/export.json"]),
            [
                "--json",
                "--session",
                "lb",
                "--cdp",
                "18810",
                "state",
                "save",
                "/tmp/lb-1/export.json"
            ]
        );
    }

    #[test]
    fn storage_scripts_check_the_origin_before_touching_anything() {
        let read = read_script("https://a.test");
        assert!(
            read.starts_with(r#"(() => { if (location.origin !== "https://a.test") return null;"#),
            "{read}"
        );
        let tricky = vec![json!({"name": "k\"); alert(1); (\"", "value": "</script>\u{2028}"})];
        let replace = replace_script("https://a.test", &tricky);
        let check = replace.find("location.origin !==").unwrap();
        assert!(check < replace.find("localStorage.clear()").unwrap());
        assert!(
            replace.contains(r#"k\"); alert(1); (\""#),
            "values are JSON-escaped: {replace}"
        );
    }

    #[test]
    fn keeps_only_string_storage_pairs_and_collects_tab_origins() {
        let items =
            storage_items(&json!([{"name": "a", "value": "1"}, {"name": "b", "value": 2}, "x"]))
                .unwrap();
        assert_eq!(items, vec![json!({"name": "a", "value": "1"})]);
        assert!(
            storage_items(&Value::Null).is_none(),
            "a redirected page reads as not read"
        );
        let mut origins = BTreeSet::new();
        tab_origins(
            &json!({"tabs": [
                {"tabId": "t1", "url": "https://a.test/path?q", "targetId": "X"},
                {"tabId": "t2", "url": "chrome://newtab/"},
                {"tabId": "t3", "url": "http://b.test:8080/"}
            ]}),
            &mut origins,
        );
        assert_eq!(
            origins.into_iter().collect::<Vec<_>>(),
            ["http://b.test:8080", "https://a.test"]
        );
    }

    #[test]
    fn scratch_is_private_and_removed() {
        use std::os::unix::fs::PermissionsExt;
        let scratch = Scratch::new().unwrap();
        let dir = scratch.0.clone();
        assert_eq!(
            std::fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let file = scratch.write("cookies.json", b"{}").unwrap();
        assert_eq!(
            std::fs::metadata(&file).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(
            scratch.write("cookies.json", b"{}").is_err(),
            "never reuses a file"
        );
        drop(scratch);
        assert!(!dir.exists());
    }
}
