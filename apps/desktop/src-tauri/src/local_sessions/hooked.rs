//! Cursor and Grok CLI (agent network §3, §4, phase 2). Neither has a session registry or a
//! channel we can push into, so:
//! - a session exists from its first hook: the hook writes a record, and discovery reads it;
//! - a message waits in the session's inbox, and its next hook (the next tool call or turn end)
//!   hands it to the agent in the shape that agent reads. The hold, loop limit, label and defang
//!   are the backend's and `Command::native_text`'s, as for Claude and Codex.
//!
//! Hook formats are ported from reason-network's `hook.rs`/`setup.rs`.

use super::*;

/// The two agents this module serves.
pub(crate) fn provider(name: &str) -> bool {
    matches!(name, "cursor" | "grok")
}

/// Records and inboxes, beside the wake socket (private, this user's).
pub(super) fn dir() -> PathBuf {
    enrollment::hook_path().with_file_name("hooked")
}

#[derive(Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Record {
    provider: String,
    conversation: String,
    profile: String,
    pid: u32,
    birth_micros: u64,
    cwd: Option<String>,
    status: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Letter {
    text: String,
    /// The send's journal receipt and request id: the hook that hands the letter over settles it
    /// `applied`. Until then the receipt stays `unknown` (the agent may never take it).
    #[serde(default)]
    receipt: Option<(PathBuf, String)>,
}

fn key(provider: &str, conversation: &str) -> String {
    fingerprint(&json!([provider, conversation]).to_string())
}

fn inbox(dir: &Path, provider: &str, conversation: &str) -> PathBuf {
    dir.join(format!("{}.inbox", key(provider, conversation)))
}

/// The hook's stdin in the shape the rest of the pipeline reads (Claude's names), or None for a
/// call to ignore. `event` is the one our hook command names (`post-tool-use`): the payloads'
/// own names differ by agent and version.
pub(super) fn normalize(provider: &str, event: &str, mut json: Value) -> Option<Value> {
    let canonical = match event {
        "session-start" => "SessionStart",
        "user-prompt-submit" => "UserPromptSubmit",
        "post-tool-use" => "PostToolUse",
        "stop" => "Stop",
        "session-end" => "SessionEnd",
        _ => return None,
    };
    // Grok runs a subagent's hooks under the subagent's own session id, and after SessionEnd it
    // fires one more Stop (reason "shutdown" or "channel_closed") whose output it ignores.
    if provider == "grok" && (!json["subagentType"].is_null() || (canonical == "Stop" && json["reason"] != "end_turn")) {
        return None;
    }
    let text = |k: &str| json[k].as_str().filter(|s| !s.is_empty() && s.len() <= 128).map(str::to_owned);
    let session = text("session_id").or_else(|| text("conversation_id")).or_else(|| text("sessionId"))?;
    let cwd = json["cwd"].as_str().or_else(|| json["workspace_roots"][0].as_str()).map(str::to_owned);
    let object = json.as_object_mut()?;
    object.insert("hook_event_name".into(), json!(canonical));
    object.insert("session_id".into(), json!(session));
    if let Some(cwd) = cwd {
        object.insert("cwd".into(), json!(cwd));
    }
    Some(json)
}

/// Whether this hook's output reaches the model. Cursor's and Grok's prompt hooks cannot add
/// context, and Grok drops what SessionStart prints: those hooks leave the inbox alone.
pub(super) fn takes_mail(provider: &str, event: &str) -> bool {
    !matches!((provider, event), (_, "UserPromptSubmit" | "SessionEnd") | ("grok", "SessionStart"))
}

/// What each agent reads from this hook to put `text` in front of the model.
pub(super) fn output(provider: &str, event: &str, text: &str) -> Value {
    match (provider, event) {
        ("cursor", "Stop") => json!({"followup_message": text}),
        ("cursor", _) => json!({"additional_context": text}),
        ("grok", "Stop") => json!({"decision": "block", "reason": text}),
        (_, name) => json!({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}),
    }
}

fn status(event: &str) -> &'static str {
    match event {
        "UserPromptSubmit" | "PostToolUse" => "busy",
        _ => "idle",
    }
}

/// Hook process only: the session this hook is from, so discovery finds it. Its end removes it.
pub(super) fn record(dir: &Path, provider: &str, json: &Value, source: Option<&lifecycle::HookProcess>, home: &Path) -> Result<()> {
    let conversation = json["session_id"].as_str().ok_or("missing session")?;
    let path = dir.join(format!("{}.json", key(provider, conversation)));
    let event = json["hook_event_name"].as_str().unwrap_or("");
    if event == "SessionEnd" {
        let _ = fs::remove_file(&path);
        let _ = fs::remove_dir_all(inbox(dir, provider, conversation));
        return Ok(());
    }
    let source = source.ok_or("hook source unidentified")?;
    private_dir(dir)?;
    private_write(&path, &Record {
        provider: provider.into(),
        conversation: conversation.into(),
        profile: fingerprint(&home.join(format!(".{provider}")).to_string_lossy()),
        pid: source.pid,
        birth_micros: source.birth_micros,
        cwd: json["cwd"].as_str().map(str::to_owned),
        status: status(event).into(),
    })
}

/// Every recorded session whose agent process still runs. A dead one's record and inbox go.
pub(super) fn discover(dir: &Path) -> Vec<NativeSession> {
    let Ok(entries) = fs::read_dir(dir) else { return vec![] };
    let mut found = vec![];
    for path in entries.flatten().map(|e| e.path()) {
        let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
        if path.extension().is_none_or(|e| e != "json") || stem.len() != 64 {
            continue;
        }
        let Ok(record) = private_read::<Record>(&path) else { continue };
        // A record naming a Claude or Codex process (written by a build that trusted the nearest
        // ancestor) is not this agent's: dropped, never a second session on that pid.
        let live = process(record.pid).ok().filter(|p| p.birth_micros == record.birth_micros && !lifecycle::native_agent(&p.executable));
        let (Some(live), true) = (live, provider(&record.provider)) else {
            let _ = fs::remove_file(&path);
            let _ = fs::remove_dir_all(inbox(dir, &record.provider, &record.conversation));
            continue;
        };
        found.push(NativeSession {
            provider: record.provider,
            conversation: record.conversation,
            profile: record.profile,
            process: live,
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "hook".into(),
            restriction: None,
            status: record.status,
            title: None,
            cwd_name: record.cwd.as_deref().and_then(|c| Path::new(c).file_name()).map(|n| n.to_string_lossy().chars().take(128).collect()),
            active_turn: None,
            last_assistant_message: None,
            holder: None,
            direct_holder: false,
            job: None,
            forked_from: None,
            forked_by_person: None,
            aliases: vec![],
            app: false,
            attention: Default::default(),
        });
    }
    found
}

/// Verification for a hooked session: its own process still runs. There is nothing else to read.
pub(super) fn verify(native: &NativeSession) -> Result<NativeSession> {
    match process(native.process.pid) {
        Ok(current) if current == native.process => Ok(native.clone()),
        _ => Err("native identity changed".into()),
    }
}

/// Delivery: the framed text waits in the session's inbox for its next hook. Its receipt (at
/// `receipt`) stays `unknown` until that hook hands it over and settles it `applied`; a session
/// whose process ends first never took it (`transport` then rejects it).
pub(super) fn deliver(dir: &Path, native: &NativeSession, request_id: &str, text: &str, receipt: Option<&Path>) -> Result<()> {
    let inbox = inbox(dir, &native.provider, &native.conversation);
    private_dir(dir)?;
    private_dir(&inbox)?;
    let at = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|e| e.to_string())?.as_nanos();
    let letter = Letter { text: text.into(), receipt: receipt.map(|p| (p.to_path_buf(), request_id.to_owned())) };
    private_write(&inbox.join(format!("{at:024}-{}.json", &fingerprint(request_id)[..16])), &letter)
}

/// Hook process only: every letter waiting for this session, oldest first, each taken once. A
/// rename claims a letter, so two hooks running at once never both hand it over.
pub(super) fn drain(dir: &Path, provider: &str, conversation: &str) -> Vec<String> {
    let inbox = inbox(dir, provider, conversation);
    let Ok(entries) = fs::read_dir(&inbox) else { return vec![] };
    let mut names: Vec<String> = entries
        .flatten()
        .filter_map(|e| e.file_name().into_string().ok())
        .filter(|n| !n.starts_with('.') && n.ends_with(".json"))
        .collect();
    names.sort();
    let mut letters = vec![];
    for name in names {
        let claimed = inbox.join(format!(".claimed-{}", uuid::Uuid::new_v4()));
        if fs::rename(inbox.join(&name), &claimed).is_err() {
            continue; // another hook took it
        }
        if let Ok(letter) = private_read::<Letter>(&claimed) {
            if let Some((receipt, request)) = letter.receipt {
                let _ = Journal::finish_at(&receipt, Some(request), false);
            }
            letters.push(letter.text);
        }
        let _ = fs::remove_file(&claimed);
    }
    letters
}

// ---- `lightbulb setup`: hooks and the MCP entry, each file backed up once before its first edit.

const EVENTS: [&str; 5] = ["session-start", "user-prompt-submit", "post-tool-use", "stop", "session-end"];

fn command(executable: &Path, provider: &str, event: &str) -> Result<String> {
    Ok(format!("{} {event}", super::claude::hook_command(executable, provider)?))
}

fn ours(entry: &Value, provider: &str) -> bool {
    entry.to_string().contains(&format!("' local-session-hook {provider} "))
}

fn read_doc(path: &Path) -> Result<Option<Value>> {
    if !path.try_exists().map_err(|e| e.to_string())? {
        return Ok(None);
    }
    let doc: Value = read_json(secure_file(path)?, 1024 * 1024).map_err(|_| format!("invalid {}", path.display()))?;
    doc.is_object().then_some(Some(doc)).ok_or_else(|| format!("invalid {}", path.display()))
}

fn write_if_changed(path: &Path, original: &Option<Value>, doc: &Value) -> Result<()> {
    if original.as_ref() == Some(doc) || (original.is_none() && doc.as_object().is_some_and(|o| o.is_empty())) {
        return Ok(());
    }
    backup_once(path)?;
    let mut bytes = serde_json::to_vec_pretty(doc).map_err(|e| e.to_string())?;
    bytes.push(b'\n');
    super::codex::replace(path, &bytes)
}

/// Drop every hook entry of ours (in a group, only our handler), then add the current ones.
fn merge_hooks(hooks: &mut serde_json::Map<String, Value>, provider: &str, ours_now: Vec<(&str, Value)>) {
    for list in hooks.values_mut() {
        let Some(groups) = list.as_array_mut() else { continue };
        for group in groups.iter_mut() {
            if let Some(inner) = group.get_mut("hooks").and_then(Value::as_array_mut) {
                inner.retain(|h| !ours(h, provider));
            }
        }
        groups.retain(|g| match g.get("hooks").and_then(Value::as_array) {
            Some(inner) => !inner.is_empty(),
            None => !ours(g, provider),
        });
    }
    hooks.retain(|_, list| list.as_array().is_none_or(|a| !a.is_empty()));
    for (event, entry) in ours_now {
        if let Some(list) = hooks.entry(event).or_insert_with(|| json!([])).as_array_mut() {
            list.push(entry);
        }
    }
}

fn cursor_hooks(home: &Path, executable: &Path, enabled: bool) -> Result<()> {
    let path = home.join(".cursor/hooks.json");
    let original = read_doc(&path)?;
    let mut doc = original.clone().unwrap_or_else(|| json!({}));
    let mut ours_now = vec![];
    if enabled {
        doc.as_object_mut().ok_or("invalid Cursor hooks")?.entry("version").or_insert(json!(1));
        for (name, event) in ["sessionStart", "beforeSubmitPrompt", "postToolUse", "stop", "sessionEnd"].into_iter().zip(EVENTS) {
            let mut entry = json!({"command": command(executable, "cursor", event)?, "timeout": 10});
            if event == "post-tool-use" {
                entry["matcher"] = json!("*");
            }
            ours_now.push((name, entry));
        }
    }
    if doc.get("hooks").is_none() && enabled {
        doc["hooks"] = json!({});
    }
    if let Some(hooks) = doc.get_mut("hooks").and_then(Value::as_object_mut) {
        merge_hooks(hooks, "cursor", ours_now);
    }
    write_if_changed(&path, &original, &doc)
}

fn grok_hooks(home: &Path, executable: &Path, enabled: bool) -> Result<()> {
    let path = home.join(".grok/hooks/lightbulb.json");
    let original = read_doc(&path)?;
    let mut doc = original.clone().unwrap_or_else(|| json!({}));
    let mut ours_now = vec![];
    if enabled {
        for (name, event) in ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"].into_iter().zip(EVENTS) {
            let timeout = if event == "session-end" { 3 } else { 10 };
            let mut group = json!({"hooks": [{"type": "command", "command": command(executable, "grok", event)?, "timeout": timeout}]});
            if event == "post-tool-use" {
                group["matcher"] = json!("*");
            }
            ours_now.push((name, group));
        }
        private_or_existing_dir(&home.join(".grok/hooks"))?;
    }
    if doc.get("hooks").is_none() && enabled {
        doc["hooks"] = json!({});
    }
    if let Some(hooks) = doc.get_mut("hooks").and_then(Value::as_object_mut) {
        merge_hooks(hooks, "grok", ours_now);
    }
    write_if_changed(&path, &original, &doc)
}

fn private_or_existing_dir(path: &Path) -> Result<()> {
    if path.is_dir() {
        return Ok(());
    }
    use std::os::unix::fs::DirBuilderExt;
    fs::DirBuilder::new().recursive(true).mode(0o700).create(path).map_err(|e| e.to_string())
}

/// Who holds the `lightbulb` MCP name: nobody, this executable's `session-mcp` (command AND args),
/// Lightbulb's hosted server (replaced, as for Claude and Codex), or another tool (left alone).
#[derive(PartialEq)]
enum Slot {
    Absent,
    Ours,
    Hosted,
    Foreign,
}

fn slot(present: bool, command: Option<&str>, args: &[&str], url: Option<&str>, executable: &Path) -> Slot {
    match () {
        _ if !present => Slot::Absent,
        _ if command == executable.to_str() && args == ["session-mcp"] => Slot::Ours,
        _ if command.is_none() && url.is_some_and(|u| u.contains("thelightbulb.company/mcp")) => Slot::Hosted,
        _ => Slot::Foreign,
    }
}

fn foreign_note(agent: &str) -> String {
    format!("{agent} already has a lightbulb MCP server that is not Lightbulb's; it was left as it is")
}

/// `~/.cursor/mcp.json`'s `mcpServers.lightbulb`: this executable's `session-mcp`.
fn cursor_mcp(home: &Path, executable: &Path, enabled: bool, notes: &mut Vec<String>) -> Result<()> {
    let path = home.join(".cursor/mcp.json");
    let original = read_doc(&path)?;
    let mut doc = original.clone().unwrap_or_else(|| json!({}));
    let current = doc["mcpServers"]["lightbulb"].clone();
    let args: Vec<&str> = current["args"].as_array().map(|a| a.iter().filter_map(Value::as_str).collect()).unwrap_or_default();
    let held = slot(!current.is_null(), current["command"].as_str(), &args, current["url"].as_str(), executable);
    // The allow rule goes with our entry only: never beside another tool's `lightbulb`, and taken
    // back when one replaced ours (its tools would run unasked).
    let allow = enabled && held != Slot::Foreign;
    match (enabled, held) {
        (true, Slot::Foreign) => notes.push(foreign_note("cursor")),
        (true, Slot::Ours) => {}
        (true, held) => {
            if held == Slot::Hosted {
                notes.push(format!("cursor: {}", super::native_mcp::REPLACED_NOTE));
            }
            let servers = doc.as_object_mut().ok_or("invalid Cursor MCP config")?.entry("mcpServers").or_insert_with(|| json!({}));
            servers.as_object_mut().ok_or("invalid Cursor MCP config")?.insert(
                "lightbulb".into(),
                json!({"type": "stdio", "command": executable.to_string_lossy(), "args": ["session-mcp"]}),
            );
        }
        (false, Slot::Ours) => {
            if let Some(servers) = doc.get_mut("mcpServers").and_then(Value::as_object_mut) {
                servers.remove("lightbulb");
            }
        }
        (false, _) => {}
    }
    write_if_changed(&path, &original, &doc)?;
    cursor_allow(home, allow)
}

/// Cursor CLI's rule for every tool of the `lightbulb` server (its matcher takes `*` for the tool).
const CURSOR_ALLOW: &str = "Mcp(lightbulb:*)";

/// `~/.cursor/cli-config.json`'s `permissions.allow` rule without which Cursor CLI asks before every
/// lightbulb call. Only a file Cursor CLI wrote: before its first run there is nothing to add to,
/// and the next re-assert adds it. Off removes only our rule.
fn cursor_allow(home: &Path, add: bool) -> Result<()> {
    let path = home.join(".cursor/cli-config.json");
    let Some(original) = read_doc(&path)? else { return Ok(()) };
    let mut doc = original.clone();
    if add {
        let permissions = doc.as_object_mut().ok_or("invalid Cursor CLI config")?.entry("permissions").or_insert_with(|| json!({"allow": [], "deny": []}));
        let allow = permissions.as_object_mut().ok_or("invalid Cursor CLI config")?.entry("allow").or_insert_with(|| json!([]));
        let allow = allow.as_array_mut().ok_or("invalid Cursor CLI config")?;
        if !allow.iter().any(|v| v == CURSOR_ALLOW) {
            allow.push(json!(CURSOR_ALLOW));
        }
    } else if let Some(allow) = doc.pointer_mut("/permissions/allow").and_then(Value::as_array_mut) {
        allow.retain(|v| v != CURSOR_ALLOW);
    }
    write_if_changed(&path, &Some(original), &doc)
}

const GROK_ALLOW: &str = "MCPTool(lightbulb__*)";

/// `~/.grok/config.toml`: `[mcp_servers.lightbulb]`, and the `permission.allow` rule without which
/// Grok asks before every lightbulb call. Neither is written beside another tool's `lightbulb`.
fn grok_mcp(home: &Path, executable: &Path, enabled: bool, notes: &mut Vec<String>) -> Result<()> {
    let path = home.join(".grok/config.toml");
    let original = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e.to_string()),
    };
    let mut doc: toml_edit::DocumentMut = original.parse().map_err(|_| "invalid Grok config")?;
    let current = doc.get("mcp_servers").and_then(|s| s.get("lightbulb"));
    let text = |k: &str| current.and_then(|s| s.get(k)).and_then(toml_edit::Item::as_str).map(str::to_owned);
    let args: Vec<String> = current
        .and_then(|s| s.get("args"))
        .and_then(toml_edit::Item::as_array)
        .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_owned)).collect())
        .unwrap_or_default();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    let held = slot(current.is_some(), text("command").as_deref(), &args, text("url").as_deref(), executable);
    if enabled && held == Slot::Foreign {
        notes.push(foreign_note("grok"));
    } else if enabled {
        if held == Slot::Hosted {
            notes.push(format!("grok: {}", super::native_mcp::REPLACED_NOTE));
        }
        if held != Slot::Ours {
            let servers = doc.entry("mcp_servers").or_insert(toml_edit::table());
            if let Some(t) = servers.as_table_mut() {
                t.set_implicit(true);
            }
            let mut table = toml_edit::Table::new();
            table["command"] = toml_edit::value(executable.to_string_lossy().into_owned());
            table["args"] = toml_edit::value(toml_edit::Array::from_iter(["session-mcp"]));
            table["enabled"] = toml_edit::value(true);
            servers["lightbulb"] = toml_edit::Item::Table(table);
        }
        let allow = doc.entry("permission").or_insert(toml_edit::table())["allow"]
            .or_insert(toml_edit::value(toml_edit::Array::new()));
        if let Some(allow) = allow.as_array_mut() {
            if !allow.iter().any(|v| v.as_str() == Some(GROK_ALLOW)) {
                allow.push(GROK_ALLOW);
            }
        }
    } else if held == Slot::Ours {
        if let Some(servers) = doc.get_mut("mcp_servers").and_then(|t| t.as_table_like_mut()) {
            servers.remove("lightbulb");
        }
    }
    // Our rule goes when we are off, and when another tool's `lightbulb` replaced ours: its tools
    // would otherwise run unasked.
    if !enabled || held == Slot::Foreign {
        if let Some(permission) = doc.get_mut("permission").and_then(|p| p.as_table_like_mut()) {
            if let Some(allow) = permission.get_mut("allow").and_then(|a| a.as_array_mut()) {
                allow.retain(|v| v.as_str() != Some(GROK_ALLOW));
                if allow.is_empty() {
                    permission.remove("allow");
                }
            }
            if permission.is_empty() {
                doc.remove("permission");
            }
        }
    }
    let updated = doc.to_string();
    if updated == original {
        return Ok(());
    }
    backup_once(&path)?;
    super::codex::replace(&path, updated.as_bytes())
}

/// `lightbulb setup` (and every re-assert) for Cursor and Grok CLI, each where it is installed
/// (its `~/.cursor` or `~/.grok`). Off removes only what we wrote.
pub(super) fn configure(home: &Path, executable: &Path, enabled: bool) -> Vec<String> {
    let mut notes = vec![];
    for (name, installed) in [("Cursor", home.join(".cursor").is_dir()), ("Grok CLI", home.join(".grok").is_dir())] {
        if !installed {
            continue;
        }
        let result = if name == "Cursor" {
            cursor_hooks(home, executable, enabled).and_then(|()| cursor_mcp(home, executable, enabled, &mut notes))
        } else {
            grok_hooks(home, executable, enabled).and_then(|()| grok_mcp(home, executable, enabled, &mut notes))
        };
        if let Err(reason) = result {
            notes.push(format!("{name} was not set up ({reason})"));
        }
    }
    notes
}

#[cfg(test)]
mod tests {
    use super::*;

    const EXE: &str = "/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop";

    fn private_tmp() -> tempfile::TempDir {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        fs::set_permissions(dir.path(), fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }

    #[test]
    fn hooked_normalize_names_events_like_claude_and_skips_what_grok_ignores() {
        let cursor = normalize("cursor", "post-tool-use", json!({"conversation_id": "c-1", "workspace_roots": ["/w/api"]})).unwrap();
        assert_eq!(cursor["hook_event_name"], "PostToolUse");
        assert_eq!(cursor["session_id"], "c-1");
        assert_eq!(cursor["cwd"], "/w/api");
        assert_eq!(normalize("grok", "stop", json!({"session_id": "g", "reason": "end_turn"})).unwrap()["hook_event_name"], "Stop");
        assert!(normalize("grok", "stop", json!({"session_id": "g", "reason": "shutdown"})).is_none());
        assert!(normalize("grok", "stop", json!({"session_id": "g", "reason": "channel_closed"})).is_none());
        assert!(normalize("grok", "post-tool-use", json!({"session_id": "g", "subagentType": "general-purpose"})).is_none());
        assert!(normalize("grok", "post-tool-use", json!({"session_id": "g", "subagentType": null})).is_some());
        assert!(normalize("grok", "session-end", json!({"session_id": "g", "reason": "shutdown"})).is_some());
        assert!(normalize("cursor", "stop", json!({})).is_none(), "no session, no event");
        assert!(normalize("cursor", "bogus", json!({"session_id": "c"})).is_none());
    }

    #[test]
    fn hooked_output_is_what_each_agent_reads() {
        assert_eq!(output("cursor", "Stop", "hi"), json!({"followup_message": "hi"}));
        assert_eq!(output("cursor", "PostToolUse", "hi"), json!({"additional_context": "hi"}));
        assert_eq!(output("cursor", "SessionStart", "hi"), json!({"additional_context": "hi"}));
        assert_eq!(output("grok", "Stop", "hi"), json!({"decision": "block", "reason": "hi"}));
        assert_eq!(output("grok", "PostToolUse", "hi")["hookSpecificOutput"]["hookEventName"], "PostToolUse");
        assert!(!takes_mail("grok", "SessionStart") && !takes_mail("grok", "UserPromptSubmit"));
        assert!(!takes_mail("cursor", "UserPromptSubmit") && takes_mail("cursor", "SessionStart"));
        assert!(takes_mail("grok", "PostToolUse") && takes_mail("grok", "Stop") && takes_mail("cursor", "Stop"));
    }

    #[test]
    fn hooked_session_exists_from_its_first_hook_and_goes_with_its_end() {
        let dir = private_tmp();
        let me = process(std::process::id()).unwrap();
        let source = lifecycle::HookProcess { pid: me.pid, birth_micros: me.birth_micros };
        let hook = normalize("cursor", "post-tool-use", json!({"conversation_id": "c-1", "cwd": "/w/api"})).unwrap();
        record(dir.path(), "cursor", &hook, Some(&source), Path::new("/Users/a")).unwrap();
        let found = discover(dir.path());
        assert_eq!(found.len(), 1);
        let s = &found[0];
        assert_eq!((s.provider.as_str(), s.conversation.as_str(), s.status.as_str()), ("cursor", "c-1", "busy"));
        assert_eq!(s.cwd_name.as_deref(), Some("api"));
        assert_eq!(s.profile, fingerprint("/Users/a/.cursor"));
        assert!(s.process == me && verify(s).is_ok());
        let end = normalize("cursor", "session-end", json!({"conversation_id": "c-1"})).unwrap();
        record(dir.path(), "cursor", &end, Some(&source), Path::new("/Users/a")).unwrap();
        assert!(discover(dir.path()).is_empty());
    }

    #[test]
    fn hooked_record_of_a_dead_process_is_dropped_with_its_inbox() {
        let dir = private_tmp();
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let p = process(child.id()).unwrap();
        let hook = normalize("grok", "session-start", json!({"session_id": "g-1"})).unwrap();
        record(dir.path(), "grok", &hook, Some(&lifecycle::HookProcess { pid: p.pid, birth_micros: p.birth_micros }), Path::new("/h")).unwrap();
        let native = discover(dir.path()).remove(0);
        deliver(dir.path(), &native, "r-1", "hello", None).unwrap();
        child.kill().unwrap();
        child.wait().unwrap();
        assert!(verify(&native).is_err());
        assert!(discover(dir.path()).is_empty());
        assert!(!inbox(dir.path(), "grok", "g-1").exists());
    }

    #[test]
    fn hooked_inbox_hands_each_letter_over_once_in_order() {
        let dir = private_tmp();
        let me = process(std::process::id()).unwrap();
        let hook = normalize("cursor", "session-start", json!({"session_id": "c-2"})).unwrap();
        record(dir.path(), "cursor", &hook, Some(&lifecycle::HookProcess { pid: me.pid, birth_micros: me.birth_micros }), Path::new("/h")).unwrap();
        let native = discover(dir.path()).remove(0);
        deliver(dir.path(), &native, "r-1", "first", None).unwrap();
        deliver(dir.path(), &native, "r-2", "second", None).unwrap();
        assert_eq!(drain(dir.path(), "cursor", "c-2"), vec!["first", "second"]);
        assert!(drain(dir.path(), "cursor", "c-2").is_empty(), "taken once");
        assert!(drain(dir.path(), "grok", "c-2").is_empty(), "another agent's inbox is its own");
    }

    /// A letter in the inbox is not a delivery (prod e2e 2026-10-22: Grok hit its usage limit and
    /// the sender read `applied`). Unknown until a hook hands it over; rejected if the agent's
    /// process ends first.
    #[test]
    fn hooked_receipt_is_applied_only_when_a_hook_hands_the_letter_over() {
        let dir = private_tmp();
        let journal = Journal { dir: dir.path().join("receipts") };
        private_dir(&journal.dir).unwrap();
        let me = process(std::process::id()).unwrap();
        let hook = normalize("grok", "session-start", json!({"session_id": "g-3"})).unwrap();
        record(dir.path(), "grok", &hook, Some(&lifecycle::HookProcess { pid: me.pid, birth_micros: me.birth_micros }), Path::new("/h")).unwrap();
        let live = discover(dir.path()).remove(0);
        let mut child = std::process::Command::new("/bin/sleep").arg("30").spawn().unwrap();
        let gone = NativeSession { conversation: "g-4".into(), process: process(child.id()).unwrap(), ..live.clone() };
        for (request, native) in [("r-1", &live), ("r-2", &gone)] {
            private_write(&journal.path("s", request), &Receipt {
                hash: None, generation: "g".into(), outcome: "unknown".into(), provider_message_id: None, held: false, settled: false,
                ingress: Some(Ingress { native: native.clone(), cursor: Cursor::default(), text: "hi".into(), workspace_revision: None }),
            }).unwrap();
            deliver(dir.path(), native, request, "hi", Some(&journal.path("s", request))).unwrap();
        }
        assert_eq!(journal.reconcile("s", "r-1", "g").unwrap().outcome, "unknown", "waiting in the inbox");
        assert_eq!(drain(dir.path(), "grok", "g-3"), vec!["hi"]);
        let taken = journal.reconcile("s", "r-1", "g").unwrap();
        assert_eq!((taken.outcome.as_str(), taken.provider_message_id.as_deref(), taken.held), ("applied", Some("r-1"), false));
        child.kill().unwrap();
        child.wait().unwrap();
        assert_eq!(journal.reconcile("s", "r-2", "g").unwrap().outcome, "rejected", "its agent ended before taking it");
    }

    #[test]
    fn hooked_setup_wires_cursor_and_grok_keeps_theirs_and_backs_up_once() {
        let home = private_tmp();
        let h = home.path();
        let exe = Path::new(EXE);
        fs::create_dir_all(h.join(".cursor")).unwrap();
        fs::create_dir_all(h.join(".grok")).unwrap();
        fs::write(h.join(".cursor/hooks.json"), r#"{"version":1,"hooks":{"stop":[{"command":"their-stop"}]}}"#).unwrap();
        fs::write(h.join(".cursor/mcp.json"), r#"{"mcpServers":{"other":{"command":"x"}}}"#).unwrap();
        fs::write(h.join(".grok/config.toml"), "model = \"grok-4\"\n[permission]\nallow = [\"Bash(ls)\"]\n").unwrap();
        assert!(configure(h, exe, true).is_empty());
        assert!(configure(h, exe, true).is_empty(), "a re-assert is a no-op");

        let hooks: Value = serde_json::from_slice(&fs::read(h.join(".cursor/hooks.json")).unwrap()).unwrap();
        assert_eq!(hooks["hooks"]["stop"][0]["command"], "their-stop");
        assert_eq!(hooks["hooks"]["stop"][1]["command"], format!("'{EXE}' local-session-hook cursor stop"));
        assert_eq!(hooks["hooks"]["postToolUse"][0]["matcher"], "*");
        assert_eq!(hooks["hooks"]["stop"].as_array().unwrap().len(), 2, "re-assert added nothing");
        let mcp: Value = serde_json::from_slice(&fs::read(h.join(".cursor/mcp.json")).unwrap()).unwrap();
        assert_eq!(mcp["mcpServers"]["lightbulb"], json!({"type":"stdio","command":EXE,"args":["session-mcp"]}));
        assert_eq!(mcp["mcpServers"]["other"]["command"], "x");

        let grok: Value = serde_json::from_slice(&fs::read(h.join(".grok/hooks/lightbulb.json")).unwrap()).unwrap();
        assert_eq!(grok["hooks"]["Stop"][0]["hooks"][0]["command"], format!("'{EXE}' local-session-hook grok stop"));
        assert_eq!(grok["hooks"]["PostToolUse"][0]["matcher"], "*");
        let config = fs::read_to_string(h.join(".grok/config.toml")).unwrap();
        assert!(config.contains("model = \"grok-4\"") && config.contains("[mcp_servers.lightbulb]"));
        assert!(config.contains("\"Bash(ls)\", \"MCPTool(lightbulb__*)\""), "{config}");
        assert!(config.contains("args = [\"session-mcp\"]"));

        // Backups hold what was there before the first edit, and are written once.
        assert!(fs::read_to_string(h.join(".cursor/hooks.json.lightbulb-backup")).unwrap().contains("their-stop"));
        assert!(!fs::read_to_string(h.join(".grok/config.toml.lightbulb-backup")).unwrap().contains("lightbulb__"));

        // Off: ours goes, theirs stays.
        assert!(configure(h, exe, false).is_empty());
        let hooks: Value = serde_json::from_slice(&fs::read(h.join(".cursor/hooks.json")).unwrap()).unwrap();
        assert_eq!(hooks["hooks"], json!({"stop":[{"command":"their-stop"}]}));
        let mcp: Value = serde_json::from_slice(&fs::read(h.join(".cursor/mcp.json")).unwrap()).unwrap();
        assert!(mcp["mcpServers"]["lightbulb"].is_null() && mcp["mcpServers"]["other"].is_object());
        let config = fs::read_to_string(h.join(".grok/config.toml")).unwrap();
        assert!(!config.contains("lightbulb") && config.contains("Bash(ls)"), "{config}");
    }

    /// Without the allow rule Cursor CLI asks before every lightbulb call (prod e2e 2026-10-22).
    #[test]
    fn hooked_setup_pre_allows_lightbulb_tools_in_cursor_cli_and_takes_only_ours_back() {
        let home = private_tmp();
        let h = home.path();
        let exe = Path::new(EXE);
        fs::create_dir_all(h.join(".cursor")).unwrap();
        assert!(configure(h, exe, true).is_empty());
        assert!(!h.join(".cursor/cli-config.json").exists(), "never a config Cursor CLI did not write");
        let theirs = json!({"version": 1, "model": {"id": "x"}, "permissions": {"allow": ["Shell(ls)", "Mcp(lightbulb:find_sessions)"], "deny": ["Shell(rm)"]}});
        fs::write(h.join(".cursor/cli-config.json"), theirs.to_string()).unwrap();
        assert!(configure(h, exe, true).is_empty());
        assert!(configure(h, exe, true).is_empty(), "a re-assert is a no-op");
        let read = || serde_json::from_slice::<Value>(&fs::read(h.join(".cursor/cli-config.json")).unwrap()).unwrap();
        let mut expected = theirs.clone();
        expected["permissions"]["allow"].as_array_mut().unwrap().push(json!("Mcp(lightbulb:*)"));
        assert_eq!(read(), expected);
        assert_eq!(serde_json::from_slice::<Value>(&fs::read(h.join(".cursor/cli-config.json.lightbulb-backup")).unwrap()).unwrap(), theirs);
        assert!(configure(h, exe, false).is_empty());
        assert_eq!(read(), theirs, "off: ours goes, theirs stays");
    }

    /// Another tool's `lightbulb` replacing ours takes our allow rule with it: its tools would
    /// otherwise run with no prompt (Codex review on #4294).
    #[test]
    fn hooked_setup_takes_the_allow_rule_back_when_a_foreign_lightbulb_replaces_ours() {
        let home = private_tmp();
        let h = home.path();
        let exe = Path::new(EXE);
        fs::create_dir_all(h.join(".cursor")).unwrap();
        fs::create_dir_all(h.join(".grok")).unwrap();
        fs::write(h.join(".cursor/cli-config.json"), r#"{"permissions":{"allow":["Shell(ls)"],"deny":[]}}"#).unwrap();
        assert!(configure(h, exe, true).is_empty());
        assert!(fs::read_to_string(h.join(".cursor/cli-config.json")).unwrap().contains(CURSOR_ALLOW));
        assert!(fs::read_to_string(h.join(".grok/config.toml")).unwrap().contains(GROK_ALLOW));
        fs::write(h.join(".cursor/mcp.json"), r#"{"mcpServers":{"lightbulb":{"command":"/opt/other","args":["serve"]}}}"#).unwrap();
        let grok = fs::read_to_string(h.join(".grok/config.toml")).unwrap().replace(EXE, "/opt/other");
        fs::write(h.join(".grok/config.toml"), grok).unwrap();
        assert_eq!(configure(h, exe, true), vec![foreign_note("cursor"), foreign_note("grok")]);
        let cli: Value = serde_json::from_slice(&fs::read(h.join(".cursor/cli-config.json")).unwrap()).unwrap();
        assert_eq!(cli["permissions"]["allow"], json!(["Shell(ls)"]));
        let config = fs::read_to_string(h.join(".grok/config.toml")).unwrap();
        assert!(!config.contains(GROK_ALLOW) && config.contains("/opt/other"), "{config}");
    }

    #[test]
    fn hooked_setup_skips_agents_that_are_not_installed_and_replaces_a_foreign_entry() {
        let home = private_tmp();
        let h = home.path();
        assert!(configure(h, Path::new(EXE), true).is_empty());
        assert!(!h.join(".cursor").exists() && !h.join(".grok").exists());
        fs::create_dir_all(h.join(".cursor")).unwrap();
        fs::write(h.join(".cursor/mcp.json"), r#"{"mcpServers":{"lightbulb":{"url":"https://api.thelightbulb.company/mcp"}}}"#).unwrap();
        let notes = configure(h, Path::new(EXE), true);
        assert_eq!(notes, vec![format!("cursor: {}", super::super::native_mcp::REPLACED_NOTE)]);
        let mcp: Value = serde_json::from_slice(&fs::read(h.join(".cursor/mcp.json")).unwrap()).unwrap();
        assert_eq!(mcp["mcpServers"]["lightbulb"]["args"], json!(["session-mcp"]));
    }

    /// Ours is this executable's command AND `session-mcp`: another tool's `lightbulb`, even one
    /// that also says `session-mcp`, is never overwritten or deleted, and Grok gets no allow rule for it.
    #[test]
    fn hooked_setup_never_touches_another_tools_lightbulb_entry() {
        for theirs in [r#"{"command":"/usr/local/bin/other","args":["serve"]}"#, r#"{"command":"/opt/other","args":["session-mcp"]}"#] {
            let home = private_tmp();
            let h = home.path();
            fs::create_dir_all(h.join(".cursor")).unwrap();
            fs::create_dir_all(h.join(".grok")).unwrap();
            let cursor = format!(r#"{{"mcpServers":{{"lightbulb":{theirs}}}}}"#);
            fs::write(h.join(".cursor/mcp.json"), &cursor).unwrap();
            let entry: Value = serde_json::from_str(theirs).unwrap();
            let args = entry["args"].as_array().unwrap().iter().map(|a| format!("{a}")).collect::<Vec<_>>().join(", ");
            let grok = format!("[mcp_servers.lightbulb]\ncommand = \"{}\"\nargs = [{args}]\n", entry["command"].as_str().unwrap());
            fs::write(h.join(".grok/config.toml"), &grok).unwrap();
            for enabled in [true, false] {
                let notes = configure(h, Path::new(EXE), enabled);
                assert_eq!(notes, if enabled { vec![foreign_note("cursor"), foreign_note("grok")] } else { vec![] });
                let mcp: Value = serde_json::from_slice(&fs::read(h.join(".cursor/mcp.json")).unwrap()).unwrap();
                assert_eq!(mcp["mcpServers"]["lightbulb"], entry, "cursor entry kept ({enabled})");
                let config = fs::read_to_string(h.join(".grok/config.toml")).unwrap();
                assert!(config.starts_with(&grok) && !config.contains(GROK_ALLOW), "{config}");
            }
        }
    }
}
