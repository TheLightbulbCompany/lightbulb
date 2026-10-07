//! Local MCP servers: read/modify/write of `nodeHost.mcp.servers` in the
//! Mac-side openclaw config.
//!
//! The file is `<state_dir>/openclaw.json`, where `state_dir` is the same
//! `app_data_dir()/openclaw-state` that `lib.rs` hands to
//! `subprocess::spawn` as `OPENCLAW_STATE_DIR`. Upstream's
//! `resolveConfigPath` (src/config/paths.ts) prefers that env var over
//! `OPENCLAW_HOME`, so this is the file the sidecar actually loads.
//!
//! There is deliberately NO second store here. `openclaw.json` IS the
//! store — openclaw reads it from disk and is the only consumer. A
//! parallel `local_mcp.json` plus a syncer would be state that can drift
//! (cf. `file_access.rs`, whose `ensure_inside_allowlist` has no callers,
//! so its store is a settings UI writing to nothing).
//!
//! Two properties this module has to hold:
//!
//! 1. **Read-modify-write, never wholesale rewrite.** The file may already
//!    carry (or later gain) `browser`, `gateway`, `agents`, … blocks that
//!    openclaw itself wrote. MCP edits touch `nodeHost.mcp.servers`; startup
//!    disables only the two unapproved node integrations below.
//! 2. **Validate before writing.** openclaw 8.1's schema is strict and a
//!    rejected config does not degrade — the node host refuses to start.
//!    A settings UI that can brick the sidecar is worse than one that
//!    refuses a bad entry, so [`validate_servers`] mirrors the upstream
//!    rules that a UI can plausibly trip.
//!
//! `getRuntimeConfig()` loads once per process and there is no watcher on
//! the node-host path, so every write here must be followed by a sidecar
//! restart (see `lib.rs::restart_node_host_subprocess_locked`) or it takes
//! effect only on the next launch. The callers hold
//! `AuthState::node_host_mutation` across both the write and that restart.

use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

/// `<state_dir>/openclaw.json` — see the module docs for why this is the
/// path the sidecar reads.
pub fn config_path(state_dir: &Path) -> PathBuf {
    state_dir.join("openclaw.json")
}

/// Explicit local setup; preserve unrelated profiles, MCP and network policy.
pub fn configure_browser(state_dir: &Path, extension: bool) -> Result<(), String> {
    let mut config = read_config(state_dir)?;
    let browser = entry_object(&mut config, "browser")?;
    browser.insert("enabled".into(), Value::Bool(true));
    let profiles = entry_object(browser, "profiles")?;
    for (name, driver, port) in [("lightbulb", "openclaw", 18810), ("chrome", "extension", 18811)] {
        if name == "chrome" && !extension { continue; }
        if let Some(existing) = profiles.get(name) {
            if existing.get("driver").and_then(Value::as_str).unwrap_or("openclaw") != driver {
                return Err(format!("Existing {name} profile uses another driver; its settings were preserved."));
            }
        } else {
            profiles.insert(name.into(), serde_json::json!({"driver":driver,"cdpPort":port,"headless":false}));
        }
    }
    let proxy = entry_object(entry_object(&mut config, "nodeHost")?, "browserProxy")?;
    let mut allowed = match proxy.get("allowProfiles") {
        Some(Value::Array(list)) => list.clone(),
        Some(_) => return Err("Browser profile allowlist is malformed; settings were preserved.".into()),
        None => Vec::new(),
    };
    allowed.retain(|value| value.as_str() != Some("chrome"));
    if !allowed.iter().any(|value| value.as_str() == Some("lightbulb")) {
        allowed.push(Value::String("lightbulb".into()));
    }
    if extension { allowed.push(Value::String("chrome".into())); }
    proxy.insert("enabled".into(), Value::Bool(true));
    proxy.insert("allowProfiles".into(), Value::Array(allowed));
    write_config(state_dir, &config)
}

pub fn browser_setup_status(state_dir: &Path) -> Result<Value, String> {
    let config = Value::Object(read_config(state_dir)?);
    let allowed = config.pointer("/nodeHost/browserProxy/allowProfiles").and_then(Value::as_array);
    Ok(serde_json::json!({
        "configured": config.pointer("/browser/profiles/lightbulb").is_some(),
        "extensionEnabled": allowed.is_some_and(|list| list.iter().any(|v| v.as_str() == Some("chrome")))
    }))
}

/// Reveal only the existing app-private relay pairing, after explicit local setup.
/// The optional gateway hint is intentionally absent: our relay is node-local.
pub fn browser_pairing(state_dir: &Path) -> Result<String, String> {
    use std::io::Read;
    let config = Value::Object(read_config(state_dir)?);
    if browser_setup_status(state_dir)?["extensionEnabled"] != true {
        return Err("Enable the Chrome extension profile first.".into());
    }
    if config.pointer("/browser/profiles/chrome/driver").and_then(Value::as_str) != Some("extension") {
        return Err("The Chrome profile is not an extension profile.".into());
    }
    let port = config.pointer("/browser/profiles/chrome/cdpPort").and_then(Value::as_u64)
        .filter(|port| (1024..=65535).contains(port)).ok_or("Chrome relay port is not configured.")?;
    let credentials = state_dir.join("credentials");
    if std::fs::symlink_metadata(&credentials).map_err(|_| "Ask your agent for browser status on the chrome profile, then try again.")?.file_type().is_symlink() {
        return Err("Refusing linked credentials directory.".into());
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options.open(credentials.join("browser-extension-relay.secret"))
        .map_err(|_| "Ask your agent for browser status on the chrome profile, then try again.")?;
    if !file.metadata().map_err(|_| "Cannot inspect relay secret.")?.is_file() {
        return Err("Invalid relay secret file.".into());
    }
    let mut secret = String::new();
    file.take(66).read_to_string(&mut secret).map_err(|_| "Cannot read relay secret.")?;
    let secret = secret.trim();
    if secret.len() != 64 || !secret.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err("Invalid relay secret.".into());
    }
    Ok(format!("ws://127.0.0.1:{port}/extension#{secret}"))
}

/// Keep the app-private execution node separate from opt-in session sharing.
/// OpenClaw 9.3 advertises these plugin commands by default, exceeding the
/// paired device grant. Never widen that grant to approve local transcripts.
pub fn restrict_node_integrations(state_dir: &Path) -> Result<(), String> {
    let mut config = read_config(state_dir)?;
    let exec = entry_object(entry_object(&mut config, "tools")?, "exec")?;
    let mut changed = false;
    for (key, value) in [("security", "allowlist"), ("ask", "always")] {
        if key == "security" && exec.get(key).and_then(Value::as_str) == Some("deny") { continue; }
        if exec.get(key).and_then(Value::as_str) != Some(value) {
            exec.insert(key.into(), Value::String(value.into()));
            changed = true;
        }
    }
    let entries = entry_object(entry_object(&mut config, "plugins")?, "entries")?;
    for (plugin, feature) in [("anthropic", "sessionCatalog"), ("ollama", "nodeInference")] {
        let settings = entry_object(
            entry_object(entry_object(entries, plugin)?, "config")?,
            feature,
        )?;
        if settings.get("enabled") != Some(&Value::Bool(false)) {
            settings.insert("enabled".into(), Value::Bool(false));
            changed = true;
        }
    }
    if changed {
        write_config(state_dir, &config)?;
    }
    Ok(())
}

/// Parse the config file into an object. A missing file is an empty
/// object (a partial config is valid upstream — `validateConfigObject({nodeHost:{}})`
/// passes), a malformed one is an error: silently replacing a file we
/// failed to parse would drop whatever else the user had in it.
fn read_config(state_dir: &Path) -> Result<Map<String, Value>, String> {
    let p = config_path(state_dir);
    let bytes = match std::fs::read(&p) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Map::new()),
        Err(e) => return Err(format!("read {}: {e}", p.display())),
    };
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(Value::Object(map)) => Ok(map),
        Ok(_) => Err(format!("{} is not a JSON object", p.display())),
        Err(e) => Err(format!("parse {}: {e}", p.display())),
    }
}

/// Write via `.tmp` + rename, same discipline as `file_access::save_to_disk`,
/// so a crash mid-write can't leave a truncated config that bricks the
/// sidecar on next launch.
fn write_config(state_dir: &Path, config: &Map<String, Value>) -> Result<(), String> {
    std::fs::create_dir_all(state_dir).map_err(|e| format!("create state dir: {e}"))?;
    let p = config_path(state_dir);
    let tmp = p.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(config).map_err(|e| format!("serialize config: {e}"))?;
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = options
        .open(&tmp)
        .map_err(|e| format!("open {}: {e}", tmp.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // Also restrict a temporary file left behind by an earlier write.
        file.set_permissions(std::fs::Permissions::from_mode(0o600))
            .map_err(|e| format!("protect {}: {e}", tmp.display()))?;
    }
    file.write_all(&bytes)
        .map_err(|e| format!("write {}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, &p).map_err(|e| format!("rename into {}: {e}", p.display()))?;
    Ok(())
}

/// The current `nodeHost.mcp.servers` object, or `{}` when the file or any
/// link in that key chain is absent.
pub fn list_servers(state_dir: &Path) -> Result<Value, String> {
    let config = read_config(state_dir)?;
    Ok(config
        .get("nodeHost")
        .and_then(|v| v.get("mcp"))
        .and_then(|v| v.get("servers"))
        .filter(|v| v.is_object())
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new())))
}

/// Replace the whole servers map, preserving every other key in the file.
pub fn set_servers(state_dir: &Path, servers: Value) -> Result<(), String> {
    let servers = validate_servers(servers)?;
    let mut config = read_config(state_dir)?;
    // Each level is created only if missing, and a non-object at any level
    // is an error rather than something we silently clobber.
    let node_host = entry_object(&mut config, "nodeHost")?;
    let mcp = entry_object(node_host, "mcp")?;
    mcp.insert("servers".into(), Value::Object(servers));
    write_config(state_dir, &config)
}

/// Remove one server by key. Returns false (and writes nothing) if it
/// wasn't there.
pub fn remove_server(state_dir: &Path, name: &str) -> Result<bool, String> {
    let mut config = read_config(state_dir)?;
    let removed = config
        .get_mut("nodeHost")
        .and_then(|v| v.get_mut("mcp"))
        .and_then(|v| v.get_mut("servers"))
        .and_then(|v| v.as_object_mut())
        .and_then(|servers| servers.remove(name))
        .is_some();
    if removed {
        write_config(state_dir, &config)?;
    }
    Ok(removed)
}

/// Borrow `config[key]` as an object, inserting an empty one if absent.
fn entry_object<'a>(
    config: &'a mut Map<String, Value>,
    key: &str,
) -> Result<&'a mut Map<String, Value>, String> {
    config
        .entry(key.to_string())
        .or_insert_with(|| Value::Object(Map::new()))
        .as_object_mut()
        .ok_or_else(|| format!("openclaw.json: `{key}` exists but is not an object"))
}

const TRANSPORTS: [&str; 3] = ["stdio", "sse", "streamable-http"];

/// Mirror of the upstream rules a settings UI can plausibly trip, checked
/// here because the failure mode on the other side is silent-ish: openclaw
/// 8.1's schema is strict, and a config it rejects stops the node host from
/// starting at all.
///
/// Enforced (each traced to upstream in agent 8.1):
/// - server key non-empty, not whitespace-padded, not `__proto__`
///   (`RESERVED_MCP_SERVER_NAME`, zod-schema.root-support.ts)
/// - `disabled` rejected in favour of `enabled` (config-misc.test.ts:74 —
///   the schema rejects it with "enabled: false" as the canonical form)
/// - exactly one of `command` / `url`
/// - `transport` one of stdio|sse|streamable-http; `stdio` needs a command
///   (zod-schema.root-support.ts:390)
/// - a url-only entry gets `transport: "streamable-http"` written in.
///   Without it, `resolveMcpTransportConfig` falls THROUGH to SSE
///   (mcp-transport-config.ts:270-285) — a silent wrong-protocol client.
///   Same default `applyMcpConnectionOverride` uses.
///
/// Returns the normalized map to write.
pub fn validate_servers(servers: Value) -> Result<Map<String, Value>, String> {
    let Value::Object(servers) = servers else {
        return Err("servers must be a JSON object".into());
    };
    let mut out = Map::new();
    for (name, server) in servers {
        if name.trim().is_empty() {
            return Err("server name must not be empty".into());
        }
        if name.trim() != name {
            return Err(format!("server name \"{name}\" has surrounding whitespace"));
        }
        if name == "__proto__" {
            return Err("MCP server name \"__proto__\" is reserved; rename the server".into());
        }
        let Value::Object(mut server) = server else {
            return Err(format!("server \"{name}\" must be a JSON object"));
        };

        if server.contains_key("disabled") {
            return Err(format!(
                "server \"{name}\": `disabled` is rejected by openclaw 8.1; use `enabled` (the inverse) instead"
            ));
        }
        if let Some(enabled) = server.get("enabled") {
            if !enabled.is_boolean() {
                return Err(format!("server \"{name}\": `enabled` must be a boolean"));
            }
        }

        let command = non_empty_str(server.get("command"), &name, "command")?;
        let url = non_empty_str(server.get("url"), &name, "url")?;
        match (command, url) {
            (Some(_), Some(_)) => {
                return Err(format!(
                    "server \"{name}\": set either `command` (stdio) or `url` (http), not both"
                ))
            }
            (None, None) => return Err(format!("server \"{name}\": needs a `command` or a `url`")),
            _ => {}
        }

        if let Some(url) = url {
            if !(url.starts_with("http://") || url.starts_with("https://")) {
                return Err(format!(
                    "server \"{name}\": `url` must be http:// or https://"
                ));
            }
        }

        match server.get("transport") {
            Some(Value::String(t)) if TRANSPORTS.contains(&t.as_str()) => {
                if t == "stdio" && command.is_none() {
                    return Err(format!(
                        "server \"{name}\": \"stdio\" transport requires a non-empty command"
                    ));
                }
                if t != "stdio" && command.is_some() {
                    return Err(format!(
                        "server \"{name}\": a command-bearing server is always stdio; drop `transport`"
                    ));
                }
            }
            Some(_) => {
                return Err(format!(
                    "server \"{name}\": `transport` must be one of {}",
                    TRANSPORTS.join(", ")
                ))
            }
            // url-only with no transport would silently become SSE.
            None if url.is_some() => {
                server.insert("transport".into(), Value::String("streamable-http".into()));
            }
            None => {}
        }

        out.insert(name, Value::Object(server));
    }
    Ok(out)
}

/// `None` if the field is absent, `Some(&str)` if it is a non-empty string,
/// an error for anything else (including a blank string — upstream treats a
/// whitespace-only command as "no command").
fn non_empty_str<'a>(
    value: Option<&'a Value>,
    name: &str,
    field: &str,
) -> Result<Option<&'a str>, String> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if !s.trim().is_empty() => Ok(Some(s.as_str())),
        Some(Value::String(_)) => Err(format!("server \"{name}\": `{field}` must not be empty")),
        Some(_) => Err(format!("server \"{name}\": `{field}` must be a string")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn dir() -> tempfile::TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    #[test]
    fn node_scope_preserves_other_config_and_disables_unapproved_integrations() {
        let d = dir();
        let mut expected = json!({
            "browser": {"headless": true},
            "nodeHost": {"mcp": {"servers": {"docs": {"command": "docs-mcp"}}}},
            "plugins": {"entries": {
                "anthropic": {"config": {"sessionCatalog": {"enabled": true}}},
                "ollama": {"config": {"nodeInference": {"enabled": true}, "discovery": {"enabled": true}}},
                "browser": {"enabled": true}
            }}
        });
        std::fs::write(config_path(d.path()), expected.to_string()).unwrap();
        restrict_node_integrations(d.path()).unwrap();
        expected["tools"] = json!({"exec": {"security": "allowlist", "ask": "always"}});
        expected["plugins"]["entries"]["anthropic"]["config"]["sessionCatalog"]["enabled"] =
            json!(false);
        expected["plugins"]["entries"]["ollama"]["config"]["nodeInference"]["enabled"] =
            json!(false);
        assert_eq!(Value::Object(read_config(d.path()).unwrap()), expected);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(config_path(d.path()))
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        let bytes = std::fs::read(config_path(d.path())).unwrap();
        restrict_node_integrations(d.path()).unwrap();
        assert_eq!(std::fs::read(config_path(d.path())).unwrap(), bytes);
    }

    #[test]
    fn node_scope_creates_missing_config_but_never_overwrites_malformed_config() {
        let d = dir();
        restrict_node_integrations(d.path()).unwrap();
        let cfg = Value::Object(read_config(d.path()).unwrap());
        assert_eq!(
            cfg["plugins"]["entries"]["anthropic"]["config"]["sessionCatalog"]["enabled"],
            json!(false)
        );
        assert_eq!(
            cfg["plugins"]["entries"]["ollama"]["config"]["nodeInference"]["enabled"],
            json!(false)
        );
        for bad in [
            "{not json",
            r#"{"plugins":[]}"#,
            r#"{"plugins":{"entries":{"ollama":{"config":false}}}}"#,
        ] {
            std::fs::write(config_path(d.path()), bad).unwrap();
            assert!(restrict_node_integrations(d.path()).is_err());
            assert_eq!(std::fs::read_to_string(config_path(d.path())).unwrap(), bad);
        }
    }

    #[test]
    fn restrictive_exec_preserves_explicit_deny_and_unrelated_rules() {
        let d = dir();
        let config = json!({"tools":{"exec":{"security":"deny","ask":"off","safeBins":["true"]}}});
        std::fs::write(config_path(d.path()), config.to_string()).unwrap();
        restrict_node_integrations(d.path()).unwrap();
        let config = Value::Object(read_config(d.path()).unwrap());
        assert_eq!(config["tools"]["exec"], json!({"security":"deny","ask":"always","safeBins":["true"]}));
    }

    #[cfg(unix)]
    #[test]
    fn config_write_protects_leftover_temp_and_rejects_symlink() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let d = dir();
        let tmp = config_path(d.path()).with_extension("json.tmp");
        std::fs::write(&tmp, "leftover").unwrap();
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o644)).unwrap();
        restrict_node_integrations(d.path()).unwrap();
        assert_eq!(
            std::fs::metadata(config_path(d.path()))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        let before = std::fs::read(config_path(d.path())).unwrap();
        let target = d.path().join("untouched");
        std::fs::write(&target, "keep").unwrap();
        symlink(&target, &tmp).unwrap();
        assert!(set_servers(d.path(), json!({})).is_err());
        assert_eq!(std::fs::read(config_path(d.path())).unwrap(), before);
        assert_eq!(std::fs::read_to_string(target).unwrap(), "keep");
    }

    #[test]
    fn list_is_empty_when_file_missing() {
        let d = dir();
        assert_eq!(list_servers(d.path()).unwrap(), json!({}));
    }

    #[test]
    fn list_is_empty_when_key_missing() {
        let d = dir();
        std::fs::write(config_path(d.path()), r#"{"browser":{"headless":true}}"#).unwrap();
        assert_eq!(list_servers(d.path()).unwrap(), json!({}));
    }

    /// The whole point of read-modify-write: anything else in the file —
    /// including keys openclaw adds later at runtime — must survive.
    #[test]
    fn set_preserves_unrelated_keys() {
        let d = dir();
        std::fs::write(
            config_path(d.path()),
            r#"{"browser":{"headless":true},"nodeHost":{"skills":{"enabled":true}},"meta":{"x":1}}"#,
        )
        .unwrap();

        set_servers(d.path(), json!({"docs": {"command": "docs-mcp"}})).unwrap();

        let raw = std::fs::read(config_path(d.path())).unwrap();
        let cfg: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(cfg["browser"]["headless"], json!(true));
        assert_eq!(cfg["meta"]["x"], json!(1));
        assert_eq!(cfg["nodeHost"]["skills"]["enabled"], json!(true));
        assert_eq!(
            cfg["nodeHost"]["mcp"]["servers"]["docs"]["command"],
            json!("docs-mcp")
        );
        assert_eq!(
            list_servers(d.path()).unwrap(),
            json!({"docs":{"command":"docs-mcp"}})
        );
    }

    #[test]
    fn set_creates_the_file_and_key_chain() {
        let d = dir();
        set_servers(d.path(), json!({"docs": {"command": "docs-mcp"}})).unwrap();
        assert_eq!(
            list_servers(d.path()).unwrap(),
            json!({"docs":{"command":"docs-mcp"}})
        );
        assert!(!config_path(d.path()).with_extension("json.tmp").exists());
    }

    #[test]
    fn set_replaces_the_whole_map() {
        let d = dir();
        set_servers(
            d.path(),
            json!({"a": {"command": "a"}, "b": {"command": "b"}}),
        )
        .unwrap();
        set_servers(d.path(), json!({"b": {"command": "b2"}})).unwrap();
        assert_eq!(
            list_servers(d.path()).unwrap(),
            json!({"b":{"command":"b2"}})
        );
    }

    #[test]
    fn remove_reports_absent_and_writes_nothing() {
        let d = dir();
        assert!(!remove_server(d.path(), "nope").unwrap());
        assert!(
            !config_path(d.path()).exists(),
            "must not create a file to remove nothing"
        );

        set_servers(d.path(), json!({"a": {"command": "a"}})).unwrap();
        assert!(!remove_server(d.path(), "b").unwrap());
        assert!(remove_server(d.path(), "a").unwrap());
        assert_eq!(list_servers(d.path()).unwrap(), json!({}));
    }

    #[test]
    fn refuses_to_clobber_an_unparseable_config() {
        let d = dir();
        std::fs::write(config_path(d.path()), "{not json").unwrap();
        assert!(list_servers(d.path()).is_err());
        assert!(set_servers(d.path(), json!({})).is_err());
        assert!(remove_server(d.path(), "a").is_err());
        // Original bytes untouched.
        assert_eq!(
            std::fs::read_to_string(config_path(d.path())).unwrap(),
            "{not json"
        );
    }

    #[test]
    fn url_only_entry_gets_an_explicit_transport() {
        // Without this, resolveMcpTransportConfig falls through to SSE.
        let out =
            validate_servers(json!({"remote": {"url": "https://mcp.example.com/mcp"}})).unwrap();
        assert_eq!(out["remote"]["transport"], json!("streamable-http"));

        // An explicit choice is respected.
        let out = validate_servers(json!({"remote": {"url": "https://x/y", "transport": "sse"}}))
            .unwrap();
        assert_eq!(out["remote"]["transport"], json!("sse"));
    }

    #[test]
    fn rejects_the_configs_that_brick_the_sidecar() {
        let cases: &[(Value, &str)] = &[
            (json!({"a": {"command": "a", "disabled": true}}), "enabled"),
            (json!({"__proto__": {"command": "a"}}), "reserved"),
            (json!({"": {"command": "a"}}), "empty"),
            (json!({" a": {"command": "a"}}), "whitespace"),
            (json!({"a": {}}), "needs a `command` or a `url`"),
            (
                json!({"a": {"command": "a", "url": "https://x/y"}}),
                "not both",
            ),
            (json!({"a": {"url": "ftp://x/y"}}), "http"),
            (
                json!({"a": {"url": "https://x/y", "transport": "stdio"}}),
                "requires a non-empty command",
            ),
            (
                json!({"a": {"command": "a", "transport": "sse"}}),
                "always stdio",
            ),
            (
                json!({"a": {"url": "https://x/y", "transport": "webhook"}}),
                "must be one of",
            ),
            (
                json!({"a": {"command": "a", "enabled": "yes"}}),
                "must be a boolean",
            ),
            (json!({"a": {"command": ""}}), "must not be empty"),
            (json!({"a": {"command": 7}}), "must be a string"),
            (json!({"a": "just-a-string"}), "must be a JSON object"),
        ];
        for (input, needle) in cases {
            let err = validate_servers(input.clone())
                .expect_err(&format!("expected {input} to be rejected"));
            assert!(err.contains(needle), "{err:?} should mention {needle:?}");
        }
        assert!(validate_servers(json!([])).is_err());
    }

    /// A rejected map must not have touched the file.
    #[test]
    fn invalid_set_leaves_the_file_alone() {
        let d = dir();
        set_servers(d.path(), json!({"a": {"command": "a"}})).unwrap();
        assert!(set_servers(d.path(), json!({"b": {"disabled": true}})).is_err());
        assert_eq!(
            list_servers(d.path()).unwrap(),
            json!({"a":{"command":"a"}})
        );
    }
}
    #[test]
    fn browser_setup_preserves_config_and_requires_explicit_extension_opt_in() {
        let dir = tempfile::tempdir().unwrap();
        let original = serde_json::json!({"browser":{"ssrfPolicy":{"allowedHostnames":["localhost"]}},
            "nodeHost":{"mcp":{"servers":{"local":{"command":"hello"}}}}, "custom":true});
        std::fs::write(config_path(dir.path()), original.to_string()).unwrap();
        configure_browser(dir.path(), false).unwrap();
        let config = Value::Object(read_config(dir.path()).unwrap());
        assert_eq!(config["browser"]["ssrfPolicy"], original["browser"]["ssrfPolicy"]);
        assert_eq!(config["nodeHost"]["mcp"], original["nodeHost"]["mcp"]);
        assert_eq!(config["nodeHost"]["browserProxy"]["allowProfiles"], serde_json::json!(["lightbulb"]));
        assert_eq!(config["browser"]["profiles"]["lightbulb"]["headless"], false);
        configure_browser(dir.path(), true).unwrap();
        let config = Value::Object(read_config(dir.path()).unwrap());
        assert_eq!(config["nodeHost"]["browserProxy"]["allowProfiles"], serde_json::json!(["lightbulb", "chrome"]));
        assert_eq!(config["browser"]["profiles"]["chrome"]["driver"], "extension");
        assert_eq!(config["custom"], true);
        let secret = "a".repeat(64);
        assert!(browser_pairing(dir.path()).is_err());
        let credentials = dir.path().join("credentials");
        std::fs::create_dir(&credentials).unwrap();
        std::fs::write(credentials.join("browser-extension-relay.secret"), &secret).unwrap();
        assert_eq!(browser_pairing(dir.path()).unwrap(), format!("ws://127.0.0.1:18811/extension#{secret}"));
        configure_browser(dir.path(), false).unwrap();
        assert!(browser_pairing(dir.path()).is_err());
    }
