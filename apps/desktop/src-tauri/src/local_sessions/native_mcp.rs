//! The native CLI's small, session-bound Lightbulb MCP server.
//!
//! The model chooses only a destination and text. The running Mac app derives
//! the source from OS peer identity and provider-owned MCP metadata; it refuses
//! a call when that source cannot be proved against a live mirrored session.

use serde_json::{json, Value};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

const MAX_FRAME: u64 = 12 * 1024;
// A `read_messages` page carries message bodies, not just target names.
const MAX_RESPONSE_FRAME: u64 = 512 * 1024;
const SERVER_NAME: &str = "lightbulb";
/// The name earlier releases installed under; enrollment removes it.
const LEGACY_SERVER_NAME: &str = "lightbulb_native_sessions";
pub(super) const REPLACED_NOTE: &str = "Mac bridge is serving lightbulb; the hosted entry was replaced";

fn cli_home(command: &mut Command, home: &Path) {
    // The reassertion path also runs with an isolated home during CI. Provider
    // CLIs otherwise inherit the runner's real HOME and would mutate its MCP
    // settings instead of the home we are reconciling.
    if std::env::var_os("HOME").as_deref() != Some(home.as_os_str()) {
        command
            .env("HOME", home)
            .env("CODEX_HOME", home.join(".codex"))
            .env_remove("CLAUDE_CONFIG_DIR");
    }
}

/// The session browser's MCP entry (spec 2026-10-11): Google's chrome-devtools-mcp attached to this
/// Mac's session-browser port. We write no browser tools of our own.
pub(super) const BROWSER_SERVER_NAME: &str = "lightbulb-browser";

pub(super) fn browser_command() -> Vec<String> {
    vec!["npx".into(), "-y".into(), "chrome-devtools-mcp@latest".into(), "--browserUrl".into(),
        format!("http://127.0.0.1:{}", crate::session_browser::PORT)]
}

/// The MCP server entry `name` as the provider stores it (`{command, args}`); Null when absent.
fn entry(home: &Path, cli: &Path, provider: &str, name: &str) -> io::Result<Value> {
    if provider == "claude" {
        let doc = match std::fs::read(home.join(".claude.json")) {
            Ok(bytes) => serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Claude MCP config invalid"))?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Value::Null),
            Err(e) => return Err(e),
        };
        return Ok(doc["mcpServers"][name].clone());
    }
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    let output = command.args(["mcp", "get", name, "--json"]).output()?;
    if !output.status.success() {
        return Ok(Value::Null);
    }
    let doc = serde_json::from_slice::<Value>(&output.stdout)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Codex MCP config invalid"))?;
    Ok(doc["transport"].clone())
}

fn configured(home: &Path, cli: &Path, provider: &str, name: &str) -> io::Result<Option<String>> {
    let entry = entry(home, cli, provider, name)?;
    if entry.is_null() {
        return Ok(None);
    }
    if entry["args"] != json!(["session-mcp"]) {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            "MCP server name is already used",
        ));
    }
    entry["command"]
        .as_str()
        .map(|v| Some(v.to_owned()))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "MCP server command invalid"))
}

fn change(home: &Path, cli: &Path, provider: &str, verb: &str, name: &str, line: &[&std::ffi::OsStr]) -> io::Result<()> {
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    if provider == "claude" {
        command.args(["mcp", verb, "--scope", "user", name]);
    } else {
        command.args(["mcp", verb, name]);
    }
    if verb == "add" {
        command.arg("--").args(line);
    }
    if command.output()?.status.success() {
        Ok(())
    } else {
        Err(io::Error::other("native MCP config change failed"))
    }
}

/// `lightbulb-browser`: added while sharing is on, removed (only if it is ours) when it goes off.
/// A server of that name that is not ours is left alone and reported.
fn configure_browser(home: &Path, cli: &Path, provider: &str, enabled: bool, notes: &mut Vec<String>) {
    let ours = browser_command();
    let Ok(current) = entry(home, cli, provider, BROWSER_SERVER_NAME) else {
        return notes.push(format!("{provider} MCP settings are unavailable"));
    };
    let is_ours = current["command"].as_str() == Some(ours[0].as_str()) && current["args"] == json!(&ours[1..]);
    if !enabled {
        if is_ours && change(home, cli, provider, "remove", BROWSER_SERVER_NAME, &[]).is_err() {
            notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be removed"));
        }
        return;
    }
    if is_ours {
        return;
    }
    if !current.is_null() {
        return notes.push(format!("{provider} already has a {BROWSER_SERVER_NAME} server that is not Lightbulb's; it was left as it is"));
    }
    let line: Vec<&std::ffi::OsStr> = ours.iter().map(std::ffi::OsStr::new).collect();
    if change(home, cli, provider, "add", BROWSER_SERVER_NAME, &line).is_err() {
        notes.push(format!("{provider} {BROWSER_SERVER_NAME} could not be installed"));
    }
}

fn codex_config(home: &Path) -> PathBuf {
    let codex_home = if std::env::var_os("HOME").as_deref() == Some(home.as_os_str()) {
        std::env::var_os("CODEX_HOME").map(PathBuf::from).unwrap_or_else(|| home.join(".codex"))
    } else {
        home.join(".codex")
    };
    codex_home.join("config.toml")
}

/// Read-only, no CLI spawn: "configured", "missing", or "collision" (the name
/// is taken by a server that is not this executable's `session-mcp`).
pub(super) fn status(home: &Path, executable: &Path, provider: &str) -> &'static str {
    let read = |path: PathBuf| std::fs::read_to_string(path).ok();
    let (command, args) = if provider == "claude" {
        let Some(doc) = read(home.join(".claude.json")).and_then(|t| serde_json::from_str::<Value>(&t).ok()) else {
            return "missing";
        };
        let entry = &doc["mcpServers"][SERVER_NAME];
        if entry.is_null() {
            return "missing";
        }
        (entry["command"].as_str().map(str::to_owned), entry["args"] == json!(["session-mcp"]))
    } else {
        let Some(doc) = read(codex_config(home)).and_then(|t| t.parse::<toml_edit::DocumentMut>().ok()) else {
            return "missing";
        };
        let Some(server) = doc.get("mcp_servers").and_then(|s| s.get(SERVER_NAME)) else {
            return "missing";
        };
        let args = server.get("args").and_then(toml_edit::Item::as_array);
        (
            server.get("command").and_then(toml_edit::Item::as_str).map(str::to_owned),
            args.is_some_and(|a| a.len() == 1 && a.get(0).and_then(|v| v.as_str()) == Some("session-mcp")),
        )
    };
    if args && command.as_deref() == executable.to_str() { "configured" } else { "collision" }
}

fn configure_codex_policy(home: &Path, executable: &Path) -> io::Result<()> {
    let path = codex_config(home);
    let mut original = String::new();
    super::secure_file(&path)
        .map_err(io::Error::other)?
        .take(1024 * 1024)
        .read_to_string(&mut original)?;
    let mut doc: toml_edit::DocumentMut = original.parse().map_err(io::Error::other)?;
    let server = doc
        .get_mut("mcp_servers")
        .and_then(toml_edit::Item::as_table_mut)
        .and_then(|servers| servers.get_mut(SERVER_NAME))
        .and_then(toml_edit::Item::as_table_mut)
        .ok_or_else(|| io::Error::other("Codex native MCP config unavailable"))?;
    let args = server.get("args").and_then(toml_edit::Item::as_array);
    if server.get("command").and_then(toml_edit::Item::as_str)
        != executable.to_str()
        || !args.is_some_and(|args| args.len() == 1 && args.get(0).and_then(|v| v.as_str()) == Some("session-mcp"))
    {
        return Err(io::Error::other("Codex native MCP config changed"));
    }
    // Codex's own prompt gates `send_message` (owner ruling 2026-10-02). Undo
    // the bare `approval_mode = "approve"` earlier builds wrote; anything else
    // there is the user's and stays.
    if let Some(tools) = server.get_mut("tools").and_then(toml_edit::Item::as_table_mut) {
        let ours = tools.get("send_message").and_then(toml_edit::Item::as_table).is_some_and(|t| {
            t.len() == 1 && t.get("approval_mode").and_then(toml_edit::Item::as_str) == Some("approve")
        });
        if ours {
            tools.remove("send_message");
            if tools.is_empty() {
                server.remove("tools");
            }
        }
    }
    let updated = doc.to_string();
    if updated != original {
        super::codex::replace(&path, updated.as_bytes()).map_err(io::Error::other)?;
    }
    Ok(())
}

fn codex_policy(home: &Path, executable: &Path, notes: &mut Vec<String>) {
    if configure_codex_policy(home, executable).is_err() {
        notes.push("Codex native MCP settings are unavailable".into());
    }
}

/// Keep the CLI-owned configs, including other servers and credentials, intact.
/// Enabled, this Mac serves `lightbulb`: an entry it did not write (the hosted
/// HTTP server) is replaced and the replacement reported. Disabled, it removes
/// only what it wrote and restores nothing.
pub(super) fn configure(home: &Path, executable: &Path, enabled: bool) -> Vec<String> {
    let mut notes = Vec::new();
    for provider in ["claude", "codex"] {
        let Some(cli) = super::provider_program(home, provider) else {
            continue;
        };
        configure_browser(home, &cli, provider, enabled, &mut notes);
        let ours_line = [executable.as_os_str(), std::ffi::OsStr::new("session-mcp")];
        // Any `session-mcp` under the old name is ours, whatever path it named.
        if let Ok(Some(_)) = configured(home, &cli, provider, LEGACY_SERVER_NAME) {
            let _ = change(home, &cli, provider, "remove", LEGACY_SERVER_NAME, &[]);
        }
        let current = configured(home, &cli, provider, SERVER_NAME);
        let ours = matches!(&current, Ok(Some(c)) if *c == executable.to_string_lossy());
        if !enabled {
            if ours && change(home, &cli, provider, "remove", SERVER_NAME, &[]).is_err() {
                notes.push(format!("{provider} native tools could not be removed"));
            }
            continue;
        }
        // (stale: our `session-mcp` at an old app path; foreign: someone else's,
        // e.g. the hosted HTTP entry -- the only replacement worth reporting.)
        let (stale, foreign) = match current {
            _ if ours => (false, false),
            Ok(None) => (false, false),
            Ok(Some(_)) => (true, false),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (false, true),
            Err(_) => {
                notes.push(format!("{provider} MCP settings are unavailable"));
                continue;
            }
        };
        if (stale || foreign) && change(home, &cli, provider, "remove", SERVER_NAME, &[]).is_err() {
            notes.push(format!("{provider} has a different {SERVER_NAME} server that could not be replaced"));
            continue;
        }
        if !ours && change(home, &cli, provider, "add", SERVER_NAME, &ours_line).is_err() {
            notes.push(format!("{provider} native tools could not be installed"));
            continue;
        }
        if foreign {
            notes.push(format!("{provider}: {REPLACED_NOTE}"));
        }
        if provider == "codex" {
            codex_policy(home, executable, &mut notes);
        }
    }
    notes
}

fn read_frame(reader: &mut impl BufRead, limit: u64) -> io::Result<Option<Vec<u8>>> {
    let mut bytes = Vec::new();
    let n = reader.take(limit + 1).read_until(b'\n', &mut bytes)?;
    if n == 0 {
        return Ok(None);
    }
    if n as u64 > limit || bytes.last() != Some(&b'\n') {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "oversized MCP frame",
        ));
    }
    Ok(Some(bytes))
}

fn tools() -> Value {
    let read = json!({"readOnlyHint":true,"destructiveHint":false,"openWorldHint":false});
    let search = json!({"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100}},
        "additionalProperties":false});
    json!({"tools": [
        {"name":"find_rooms","description":"Find Lightbulb rooms and DMs you can read, with their members. Use a member id as person_id.",
         "inputSchema":search, "annotations":read},
        {"name":"find_sessions","description":"Find shared Claude Code and Codex sessions. This session is marked isThisSession; never message it.",
         "inputSchema":search, "annotations":read},
        {"name":"read_messages","description":"Read a room's latest messages, or continue in order after after_seq.",
         "inputSchema":{"type":"object","properties":{"room_id":{"type":"string"},"after_seq":{"type":"integer","minimum":0},
           "limit":{"type":"integer","minimum":1,"maximum":100}},"required":["room_id"],"additionalProperties":false},
         "annotations":read},
        {"name":"send_message","description":"Post one message as this session to exactly one of room_id, person_id or session_id. Use a new UUID request_id and reuse it on retry. Cannot answer permission prompts.",
         "inputSchema":{"type":"object","properties":{"room_id":{"type":"string"},"person_id":{"type":"string"},"session_id":{"type":"string"},
           "text":{"type":"string"},"request_id":{"type":"string"},"reply_to_message_id":{"type":"string","description":"Not supported from this Mac yet; omit."}},
          "required":["text","request_id"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":true,"openWorldHint":false}},
        {"name":"message_receipt","description":"Check delivery of a send_message from this session by request_id.",
         "inputSchema":{"type":"object","properties":{"request_id":{"type":"string"}},"required":["request_id"],"additionalProperties":false},
         "annotations":read},
        {"name":"find_tasks","description":"Find tasks on the workspace task board, newest-updated first. Open tasks (backlog, todo, in_progress, in_review, blocked) unless status names others (comma-separated; also done, cancelled). assignee is an agent name.",
         "inputSchema":{"type":"object","properties":{"query":{"type":"string"},"status":{"type":"string"},"assignee":{"type":"string"},
           "limit":{"type":"integer","minimum":1,"maximum":100}},"additionalProperties":false},
         "annotations":read},
        {"name":"read_task","description":"Read one task by identifier (e.g. ISOA-123) or id: status, priority, assignee, full description, blockers and latest comments.",
         "inputSchema":{"type":"object","properties":{"task":{"type":"string"},"comments":{"type":"integer","minimum":0,"maximum":50}},
           "required":["task"],"additionalProperties":false},
         "annotations":read},
        {"name":"update_task","description":"Change a task's status (backlog, todo, in_progress, in_review, blocked, done, cancelled) or priority (critical, high, medium, low), and/or append a dated note to its description. Posts no comment: a task comment wakes the assigned agent and starts a paid run.",
         "inputSchema":{"type":"object","properties":{"task":{"type":"string"},"status":{"type":"string"},"priority":{"type":"string"},
           "append_note":{"type":"string"}},"required":["task"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":false,"openWorldHint":false}},
        {"name":"use_browser","description":"Choose where this session's lightbulb-browser tools browse: \"mac\" (this Mac's Lightbulb agent Chrome, the default) or \"cloud\" (the workspace's cloud browser). It applies to this session's next browser connection: to switch now, restart the lightbulb-browser MCP server (/mcp in Claude Code).",
         "inputSchema":{"type":"object","properties":{"where":{"type":"string","enum":["mac","cloud"]}},"required":["where"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":false,"openWorldHint":false}}
    ]})
}

/// Lightbulb tools whose arguments and results stay out of the shared
/// transcript: destinations, request ids, message bodies and task contents.
/// Public and internal names; one list for the Claude and Codex scrubbers.
pub(super) const PRIVATE_TOOLS: &[&str] = &[
    "send", "receipt", "send_message", "message_receipt", "read_messages", "find_tasks", "read_task", "update_task",
];

pub(super) fn private_tool(name: &str) -> bool {
    PRIVATE_TOOLS.contains(&name)
}

/// A Claude tool name (`mcp__lightbulb__read_task`) for one of `PRIVATE_TOOLS`.
pub(super) fn private_call(tool: &str) -> bool {
    tool.strip_prefix("mcp__lightbulb__")
        .or_else(|| tool.strip_prefix(&format!("mcp__{LEGACY_SERVER_NAME}__")))
        .is_some_and(private_tool)
}

/// A path- and query-safe id: the Mac puts it in a URL.
fn id_ok(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty() && s.len() <= 128 && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_:.".contains(&b))
    })
}

/// Maps a public tool call onto the Mac app's internal one. `send`/`receipt`
/// keep the exact `{destination,text,requestId}` / `{requestId}` shapes the
/// backend's private approval digest is computed over.
pub(super) fn internal(name: &str, args: &Value) -> Option<(&'static str, Value)> {
    let args = args.as_object()?;
    let only = |keys: &[&str]| args.keys().all(|k| keys.contains(&k.as_str()));
    let opt_num = |k: &str| args.get(k).is_none_or(|v| v.as_u64().is_some());
    let opt_str = |k: &str| args.get(k).is_none_or(|v| v.as_str().is_some_and(|s| s.len() <= 256));
    match name {
        "find_rooms" | "find_sessions" if only(&["query", "limit"]) && opt_str("query") && opt_num("limit") => {
            Some((if name == "find_rooms" { "find_rooms" } else { "find_sessions" }, Value::Object(args.clone())))
        }
        "read_messages" if only(&["room_id", "after_seq", "limit"]) && args.get("room_id").is_some_and(id_ok)
            && opt_num("after_seq") && opt_num("limit") => Some(("read_messages", Value::Object(args.clone()))),
        "find_tasks" if only(&["query", "status", "assignee", "limit"]) && opt_str("query") && opt_str("status")
            && opt_str("assignee") && opt_num("limit") => Some(("find_tasks", Value::Object(args.clone()))),
        "read_task" if only(&["task", "comments"]) && args.get("task").is_some_and(id_ok) && opt_num("comments") => {
            Some(("read_task", Value::Object(args.clone())))
        }
        "update_task" if only(&["task", "status", "priority", "append_note"]) && args.get("task").is_some_and(id_ok)
            && opt_str("status") && opt_str("priority")
            && args.get("append_note").is_none_or(|v| v.as_str().is_some_and(|s| s.len() <= 4000)) =>
        {
            Some(("update_task", Value::Object(args.clone())))
        }
        "send_message" if only(&["room_id", "person_id", "session_id", "text", "request_id", "reply_to_message_id"])
            && args.get("reply_to_message_id").is_none_or(Value::is_null) =>
        {
            let mut ids = [("room", "room_id"), ("person", "person_id"), ("session", "session_id")]
                .into_iter()
                .filter_map(|(kind, key)| args.get(key).map(|v| (kind, v)));
            let (kind, id) = ids.next()?;
            if ids.next().is_some() || !id_ok(id) {
                return None;
            }
            let call = json!({"destination":format!("{kind}:{}", id.as_str()?),
                "text":args.get("text")?.as_str()?,"requestId":args.get("request_id")?.as_str()?});
            allowed("send", &call).then_some(("send", call))
        }
        "message_receipt" if only(&["request_id"]) => {
            let call = json!({"requestId":args.get("request_id")?.as_str()?});
            allowed("receipt", &call).then_some(("receipt", call))
        }
        "use_browser" if only(&["where"]) && args.get("where").and_then(Value::as_str).is_some_and(|w| w == "mac" || w == "cloud") => {
            Some(("use_browser", Value::Object(args.clone())))
        }
        _ => None,
    }
}

/// The internal `send`/`receipt` argument shapes.
pub(super) fn allowed(name: &str, args: &Value) -> bool {
    let Some(args) = args.as_object() else {
        return false;
    };
    match name {
        "send" => {
            args.len() == 3
                && ["destination", "text", "requestId"]
                    .iter()
                    .all(|k| args.get(*k).and_then(Value::as_str).is_some())
        }
        "receipt" => args.len() == 1 && args.get("requestId").and_then(Value::as_str).is_some(),
        _ => false,
    }
}

/// A Claude hook sees the public call; approval and reporting key on the
/// internal send/receipt (`mcp__lightbulb_native_sessions__*`, the name the
/// backend and the approval cards already know). Anything unmappable is left
/// alone and stays private by name.
pub(super) fn canonical_hook(json: &mut Value) {
    let Some(name) = json["tool_name"].as_str().and_then(|t| t.strip_prefix("mcp__lightbulb__")) else {
        return;
    };
    if let Some((tool @ ("send" | "receipt"), args)) = internal(name, &json["tool_input"]) {
        json["tool_name"] = json!(format!("mcp__{LEGACY_SERVER_NAME}__{tool}"));
        json["tool_input"] = args;
    }
}

fn app_call(name: &str, args: &Value, meta: &Value) -> io::Result<Value> {
    let path = super::enrollment::hook_path().with_file_name("tool.sock");
    let mut socket = UnixStream::connect(path)?;
    // A first-turn Codex call may wake verified discovery and wait for the
    // backend heartbeat before the Mac can attest its exact turn.
    socket.set_read_timeout(Some(Duration::from_secs(25)))?;
    socket.set_write_timeout(Some(Duration::from_secs(2)))?;
    let request = json!({"tool":name,"arguments":args,"meta":meta});
    let bytes = serde_json::to_vec(&request).map_err(io::Error::other)?;
    if bytes.len() as u64 >= MAX_FRAME {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "tool request too large",
        ));
    }
    socket.write_all(&bytes)?;
    socket.write_all(b"\n")?;
    let Some(reply) = read_frame(&mut BufReader::new(socket), MAX_RESPONSE_FRAME)? else {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            "Mac did not answer",
        ));
    };
    serde_json::from_slice(&reply)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid Mac reply"))
}

fn handle(request: &Value) -> Option<Value> {
    let id = request.get("id")?.clone();
    let params = &request["params"];
    let result = match request["method"].as_str().unwrap_or("") {
        "initialize" => {
            json!({"protocolVersion":params["protocolVersion"].as_str().unwrap_or("2025-03-26"),
            "capabilities":{"tools":{}},"serverInfo":{"name":"lightbulb","version":"2"}})
        }
        "ping" => json!({}),
        "tools/list" => tools(),
        "tools/call" => {
            let name = params["name"].as_str().unwrap_or("");
            let args = &params["arguments"];
            if let Some((name, args)) = internal(name, args) {
                match app_call(name, &args, &params["_meta"]) {
                    Ok(mut reply) => {
                        let is_error = reply["ok"] != true;
                        // Marks results the shared transcript must not show.
                        if private_tool(name) {
                            reply["_lightbulbNativeSessionTool"] = json!(name);
                        }
                        json!({"content":[{"type":"text","text":reply.to_string()}], "isError":is_error})
                    }
                    Err(_) => {
                        json!({"content":[{"type":"text","text":"Lightbulb on this Mac is unavailable or cannot verify this native session."}],"isError":true})
                    }
                }
            } else {
                json!({"content":[{"type":"text","text":"Invalid Lightbulb tool arguments."}],"isError":true})
            }
        }
        _ => {
            return Some(
                json!({"jsonrpc":"2.0","id":id,"error":{"code":-32601,"message":"Method not found"}}),
            )
        }
    };
    Some(json!({"jsonrpc":"2.0","id":id,"result":result}))
}

pub fn serve_mcp() {
    let stdin = io::stdin();
    let mut input = BufReader::new(stdin.lock());
    let stdout = io::stdout();
    let mut output = stdout.lock();
    loop {
        let bytes = match read_frame(&mut input, MAX_FRAME) {
            Ok(Some(bytes)) => bytes,
            Ok(None) | Err(_) => break,
        };
        let Ok(request) = serde_json::from_slice::<Value>(&bytes) else {
            continue;
        };
        // Notifications have no id, and MCP requires no response to them.
        let Some(reply) = handle(&request) else {
            continue;
        };
        let Ok(mut bytes) = serde_json::to_vec(&reply) else {
            break;
        };
        bytes.push(b'\n');
        if output
            .write_all(&bytes)
            .and_then(|_| output.flush())
            .is_err()
        {
            break;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codex_policy_removes_only_our_send_approval() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir(home.path().join(".codex")).unwrap();
        let config = home.path().join(".codex/config.toml");
        let executable = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        std::fs::write(&config, format!(
            "[mcp_servers.lightbulb]\ncommand = \"{}\"\nargs = [\"session-mcp\"]\n\
             [mcp_servers.lightbulb.tools.send_message]\napproval_mode = \"approve\"\n\
             [mcp_servers.lightbulb.tools.message_receipt]\napproval_mode = \"prompt\"\n\
             [mcp_servers.other]\ncommand = \"/bin/other\"\nargs = [\"serve\"]\n",
            executable.display()
        )).unwrap();
        configure_codex_policy(home.path(), executable).unwrap();
        let doc: toml_edit::DocumentMut = std::fs::read_to_string(&config).unwrap().parse().unwrap();
        assert!(doc["mcp_servers"]["lightbulb"]["tools"].get("send_message").is_none());
        assert_eq!(doc["mcp_servers"]["lightbulb"]["tools"]["message_receipt"]["approval_mode"].as_str(), Some("prompt"));
        assert_eq!(doc["mcp_servers"]["other"]["command"].as_str(), Some("/bin/other"));
        // A user's own policy on send_message stays.
        std::fs::write(&config, format!(
            "[mcp_servers.lightbulb]\ncommand = \"{}\"\nargs = [\"session-mcp\"]\n\
             [mcp_servers.lightbulb.tools.send_message]\napproval_mode = \"prompt\"\n",
            executable.display()
        )).unwrap();
        configure_codex_policy(home.path(), executable).unwrap();
        let doc: toml_edit::DocumentMut = std::fs::read_to_string(&config).unwrap().parse().unwrap();
        assert_eq!(doc["mcp_servers"]["lightbulb"]["tools"]["send_message"]["approval_mode"].as_str(), Some("prompt"));
    }

    #[test]
    fn exactly_the_nine_tools_and_only_send_is_destructive() {
        let listed = tools();
        let names: Vec<_> = listed["tools"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert_eq!(names, ["find_rooms", "find_sessions", "read_messages", "send_message", "message_receipt",
            "find_tasks", "read_task", "update_task", "use_browser"]);
        for tool in listed["tools"].as_array().unwrap() {
            let writes = matches!(tool["name"].as_str(), Some("send_message" | "update_task" | "use_browser"));
            assert_eq!(tool["annotations"]["readOnlyHint"], !writes);
            assert_eq!(tool["annotations"]["destructiveHint"], tool["name"] == "send_message");
            assert_eq!(tool["annotations"]["openWorldHint"], false);
        }
    }

    #[test]
    fn use_browser_takes_only_mac_or_cloud_and_never_a_session() {
        assert_eq!(internal("use_browser", &json!({"where": "cloud"})), Some(("use_browser", json!({"where": "cloud"}))));
        assert!(internal("use_browser", &json!({"where": "laptop"})).is_none());
        assert!(internal("use_browser", &json!({"where": "mac", "session_id": "x"})).is_none(), "the session is the Mac's to prove");
    }

    #[test]
    fn public_calls_map_onto_the_internal_send_and_receipt() {
        let r = "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449";
        for (key, kind) in [("room_id", "room"), ("person_id", "person"), ("session_id", "session")] {
            assert_eq!(
                internal("send_message", &json!({key:"x1","text":"hi","request_id":r})),
                Some(("send", json!({"destination":format!("{kind}:x1"),"text":"hi","requestId":r})))
            );
        }
        // Exactly one destination, no model-supplied actor, no unsupported reply.
        assert!(internal("send_message", &json!({"text":"hi","request_id":r})).is_none());
        assert!(internal("send_message", &json!({"room_id":"a","person_id":"b","text":"hi","request_id":r})).is_none());
        assert!(internal("send_message", &json!({"room_id":"a","text":"hi","request_id":r,"sourceSessionId":"other"})).is_none());
        assert!(internal("send_message", &json!({"room_id":"a","text":"hi","request_id":r,"reply_to_message_id":"m"})).is_none());
        assert!(internal("send_message", &json!({"room_id":"../x","text":"hi","request_id":r})).is_none());
        assert_eq!(internal("message_receipt", &json!({"request_id":r})), Some(("receipt", json!({"requestId":r}))));
        assert!(internal("message_receipt", &json!({"request_id":r,"actor":"owner"})).is_none());
        assert!(internal("read_messages", &json!({"room_id":"room/../x"})).is_none());
        assert!(internal("read_messages", &json!({"room_id":"r1","after_seq":3,"limit":20})).is_some());
        assert!(internal("find_rooms", &json!({})).is_some());
        assert!(internal("find_sessions", &json!({"query":"x","limit":"all"})).is_none());
        assert!(internal("list_targets", &json!({})).is_none());
        assert!(internal("read_messages", &json!({"room_id":"r1"})).is_some(), "after_seq is optional: the latest page");
        assert_eq!(internal("find_tasks", &json!({})), Some(("find_tasks", json!({}))));
        assert!(internal("read_task", &json!({"task":"ISOA-1","comments":5})).is_some());
        assert!(internal("read_task", &json!({"task":"../agents"})).is_none());
        let update = json!({"task":"ISOA-1","status":"done","append_note":"shipped"});
        assert_eq!(internal("update_task", &update), Some(("update_task", update.clone())));
        assert!(internal("update_task", &json!({"task":"ISOA-1","comment":"hi"})).is_none(), "no comment posting");

        let mut hook = json!({"tool_name":"mcp__lightbulb__send_message","tool_input":{"room_id":"x1","text":"hi","request_id":r}});
        canonical_hook(&mut hook);
        assert_eq!(hook["tool_name"], "mcp__lightbulb_native_sessions__send");
        assert_eq!(hook["tool_input"], json!({"destination":"room:x1","text":"hi","requestId":r}));
    }

    #[cfg(unix)]
    #[test]
    fn enable_replaces_a_hosted_lightbulb_entry_and_says_so() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        // A fake `claude` CLI that edits ~/.claude.json the way `claude mcp` does.
        let fake = bin.join("claude");
        std::fs::write(&fake, r#"#!/usr/bin/env python3
import json, os, sys
p = os.path.join(os.environ["HOME"], ".claude.json")
doc = json.load(open(p)) if os.path.exists(p) else {}
servers = doc.setdefault("mcpServers", {})
a = sys.argv[1:]
if a[:2] == ["mcp", "remove"]: servers.pop(a[-1], None)
elif a[:2] == ["mcp", "add"]: servers[a[4]] = {"command": a[6], "args": a[7:]}
json.dump(doc, open(p, "w"))
"#).unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        if super::super::provider_program(h, "claude") != std::fs::canonicalize(&fake).ok() {
            return; // provider discovery does not look in this temp home here
        }
        std::fs::write(h.join(".claude.json"), json!({"mcpServers":{
            "lightbulb":{"type":"http","url":"https://api.flickmeon.ai/mcp"},
            "lightbulb_native_sessions":{"command":"/old/lightbulb-desktop","args":["session-mcp"]}}}).to_string()).unwrap();
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        assert_eq!(status(h, exe, "claude"), "collision");
        let notes = configure(h, exe, true);
        assert!(notes.contains(&format!("claude: {REPLACED_NOTE}")), "{notes:?}");
        let doc: Value = serde_json::from_str(&std::fs::read_to_string(h.join(".claude.json")).unwrap()).unwrap();
        assert!(doc["mcpServers"]["lightbulb_native_sessions"].is_null());
        assert_eq!(status(h, exe, "claude"), "configured");
        // A second pass finds its own entry and reports nothing.
        assert!(!configure(h, exe, true).iter().any(|n| n.contains(REPLACED_NOTE)));
        // Disable removes what it wrote and restores nothing.
        configure(h, exe, false);
        assert_eq!(status(h, exe, "claude"), "missing");
    }

    #[cfg(unix)]
    #[test]
    fn sharing_on_adds_the_browser_entry_and_off_removes_only_ours() {
        use std::os::unix::fs::PermissionsExt;
        let home = tempfile::tempdir().unwrap();
        let h = home.path();
        let bin = h.join(".local/bin");
        std::fs::create_dir_all(&bin).unwrap();
        // A fake `claude` CLI that edits ~/.claude.json the way `claude mcp` does.
        let fake = bin.join("claude");
        std::fs::write(&fake, r#"#!/usr/bin/env python3
import json, os, sys
p = os.path.join(os.environ["HOME"], ".claude.json")
doc = json.load(open(p)) if os.path.exists(p) else {}
servers = doc.setdefault("mcpServers", {})
a = sys.argv[1:]
if a[:2] == ["mcp", "remove"]: servers.pop(a[-1], None)
elif a[:2] == ["mcp", "add"]: servers[a[4]] = {"command": a[6], "args": a[7:]}
json.dump(doc, open(p, "w"))
"#).unwrap();
        std::fs::set_permissions(&fake, std::fs::Permissions::from_mode(0o755)).unwrap();
        if super::super::provider_program(h, "claude") != std::fs::canonicalize(&fake).ok() {
            return; // like enable_replaces above: provider discovery may not look in this temp home
        }
        let exe = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        let servers = || serde_json::from_str::<Value>(&std::fs::read_to_string(h.join(".claude.json")).unwrap()).unwrap()["mcpServers"].clone();
        configure(h, exe, true);
        assert_eq!(servers()[BROWSER_SERVER_NAME], json!({"command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest", "--browserUrl", "http://127.0.0.1:39222"]}));
        configure(h, exe, false);
        assert!(servers()[BROWSER_SERVER_NAME].is_null());
        std::fs::write(h.join(".claude.json"), json!({"mcpServers": {BROWSER_SERVER_NAME: {"command": "/bin/other", "args": []}}}).to_string()).unwrap();
        let notes = configure(h, exe, true);
        assert!(notes.iter().any(|n| n.contains("not Lightbulb's")), "{notes:?}");
        assert_eq!(servers()[BROWSER_SERVER_NAME]["command"], "/bin/other");
        configure(h, exe, false);
        assert_eq!(servers()[BROWSER_SERVER_NAME]["command"], "/bin/other", "someone else's is never removed");
    }

    #[test]
    fn target_list_response_can_exceed_request_frame() {
        let mut frame = vec![b'a'; MAX_FRAME as usize + 1];
        frame.push(b'\n');
        assert!(read_frame(&mut io::Cursor::new(&frame), MAX_FRAME).is_err());
        assert_eq!(
            read_frame(&mut io::Cursor::new(&frame), MAX_RESPONSE_FRAME)
                .unwrap()
                .unwrap(),
            frame
        );
    }
}
