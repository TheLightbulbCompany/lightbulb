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
const MAX_RESPONSE_FRAME: u64 = 64 * 1024;
const SERVER_NAME: &str = "lightbulb_native_sessions";

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

fn configured(home: &Path, cli: &Path, provider: &str) -> io::Result<Option<String>> {
    let doc = if provider == "claude" {
        let path = home.join(".claude.json");
        match std::fs::read(&path) {
            Ok(bytes) => serde_json::from_slice::<Value>(&bytes).map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "Claude MCP config invalid")
            })?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(e),
        }
    } else {
        let mut command = Command::new(cli);
        cli_home(&mut command, home);
        let output = command.args(["mcp", "get", SERVER_NAME, "--json"]).output()?;
        if !output.status.success() {
            return Ok(None);
        }
        serde_json::from_slice::<Value>(&output.stdout)
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "Codex MCP config invalid"))?
    };
    let entry = if provider == "claude" {
        &doc["mcpServers"][SERVER_NAME]
    } else {
        &doc["transport"]
    };
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

fn change(home: &Path, cli: &Path, provider: &str, verb: &str, executable: &Path) -> io::Result<()> {
    let mut command = Command::new(cli);
    cli_home(&mut command, home);
    if provider == "claude" {
        command.args(["mcp", verb, "--scope", "user", SERVER_NAME]);
    } else {
        command.args(["mcp", verb, SERVER_NAME]);
    }
    if verb == "add" {
        command.arg("--").arg(executable).arg("session-mcp");
    }
    if command.output()?.status.success() {
        Ok(())
    } else {
        Err(io::Error::other("native MCP config change failed"))
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

fn configure_codex_policy(home: &Path, executable: &Path) -> io::Result<Vec<String>> {
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
    let tools = server
        .entry("tools")
        .or_insert(toml_edit::Item::Table(toml_edit::Table::new()))
        .as_table_mut()
        .ok_or_else(|| io::Error::other("Codex native MCP tool policy unavailable"))?;
    let mut notes = Vec::new();
    for name in ["send", "receipt"] {
        let tool = tools
            .entry(name)
            .or_insert(toml_edit::Item::Table(toml_edit::Table::new()))
            .as_table_mut()
            .ok_or_else(|| io::Error::other("Codex native MCP tool policy unavailable"))?;
        match tool.get("approval_mode").and_then(toml_edit::Item::as_str) {
            None if !tool.contains_key("approval_mode") => {
                // Only these two Lightbulb tools bypass Codex's own prompt.
                // `send` always prepares a private owner approval on the backend.
                tool.insert("approval_mode", toml_edit::value("approve"));
            }
            Some("approve") => {}
            _ => notes.push(format!("Codex {name} keeps its custom approval policy")),
        }
    }
    let updated = doc.to_string();
    if updated != original {
        super::codex::replace(&path, updated.as_bytes()).map_err(io::Error::other)?;
    }
    Ok(notes)
}

/// Keep the CLI-owned configs, including other servers and credentials, intact.
pub(super) fn configure(home: &Path, executable: &Path, enabled: bool) -> Vec<String> {
    let mut notes = Vec::new();
    for provider in ["claude", "codex"] {
        let Some(cli) = super::provider_program(home, provider) else {
            continue;
        };
        match configured(home, &cli, provider) {
            Ok(Some(current)) if current == executable.to_string_lossy() => {
                if !enabled && change(home, &cli, provider, "remove", executable).is_err() {
                    notes.push(format!("{provider} native tools could not be removed"));
                } else if enabled && provider == "codex" {
                    match configure_codex_policy(home, executable) {
                        Ok(policy) => notes.extend(policy),
                        Err(_) => notes.push("Codex native send approval policy is unavailable".into()),
                    }
                }
            }
            Ok(Some(_)) => notes.push(format!("{provider} has a different {SERVER_NAME} server")),
            Ok(None) if enabled => {
                if change(home, &cli, provider, "add", executable).is_err() {
                    notes.push(format!("{provider} native tools could not be installed"));
                } else if provider == "codex" {
                    match configure_codex_policy(home, executable) {
                        Ok(policy) => notes.extend(policy),
                        Err(_) => notes.push("Codex native send approval policy is unavailable".into()),
                    }
                }
            }
            Ok(None) => {}
            Err(_) => notes.push(format!("{provider} MCP settings are unavailable")),
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
    json!({"tools": [
        {"name":"list_targets","description":"List rooms, people and shared sessions reachable from this exact native session. The source is verified automatically.",
         "inputSchema":{"type":"object","properties":{},"additionalProperties":false},
         "annotations":{"readOnlyHint":true,"destructiveHint":false,"openWorldHint":false}},
        {"name":"send","description":"Request owner approval to send one Lightbulb message to an accessible room, person or shared session. Use a stable requestId for retries. Cannot answer permission prompts.",
         "inputSchema":{"type":"object","properties":{"destination":{"type":"string"},"text":{"type":"string"},"requestId":{"type":"string"}},
          "required":["destination","text","requestId"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":true,"openWorldHint":false}},
        {"name":"receipt","description":"Check delivery of a send from this native session by requestId.",
         "inputSchema":{"type":"object","properties":{"requestId":{"type":"string"}},"required":["requestId"],"additionalProperties":false},
         "annotations":{"readOnlyHint":false,"destructiveHint":false,"openWorldHint":false}}
    ]})
}

pub(super) fn allowed(name: &str, args: &Value) -> bool {
    let Some(args) = args.as_object() else {
        return false;
    };
    match name {
        "list_targets" => args.is_empty(),
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
            "capabilities":{"tools":{}},"serverInfo":{"name":"lightbulb-sessions","version":"1"}})
        }
        "ping" => json!({}),
        "tools/list" => tools(),
        "tools/call" => {
            let name = params["name"].as_str().unwrap_or("");
            let args = &params["arguments"];
            if !allowed(name, args) {
                json!({"content":[{"type":"text","text":"Invalid Lightbulb tool arguments."}],"isError":true})
            } else {
                match app_call(name, args, &params["_meta"]) {
                    Ok(mut reply) => {
                        let is_error = reply["ok"] != true;
                        if matches!(name, "send" | "receipt") {
                            reply["_lightbulbNativeSessionTool"] = json!(name);
                        }
                        json!({"content":[{"type":"text","text":reply.to_string()}], "isError":is_error})
                    }
                    Err(_) => {
                        json!({"content":[{"type":"text","text":"Lightbulb on this Mac is unavailable or cannot verify this native session."}],"isError":true})
                    }
                }
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
    fn codex_policy_preserves_unrelated_and_custom_tool_settings() {
        let home = tempfile::tempdir().unwrap();
        std::fs::create_dir(home.path().join(".codex")).unwrap();
        let config = home.path().join(".codex/config.toml");
        let executable = Path::new("/Applications/Lightbulb.app/Contents/MacOS/lightbulb-desktop");
        std::fs::write(&config, format!(
            "[mcp_servers.lightbulb_native_sessions]\ncommand = \"{}\"\nargs = [\"session-mcp\"]\n\
             [mcp_servers.lightbulb_native_sessions.tools.receipt]\napproval_mode = \"prompt\"\n\
             [mcp_servers.other]\ncommand = \"/bin/other\"\nargs = [\"serve\"]\n",
            executable.display()
        )).unwrap();
        let notes = configure_codex_policy(home.path(), executable).unwrap();
        let doc: toml_edit::DocumentMut = std::fs::read_to_string(config).unwrap().parse().unwrap();
        assert_eq!(doc["mcp_servers"]["lightbulb_native_sessions"]["tools"]["send"]["approval_mode"].as_str(), Some("approve"));
        assert_eq!(doc["mcp_servers"]["lightbulb_native_sessions"]["tools"]["receipt"]["approval_mode"].as_str(), Some("prompt"));
        assert_eq!(doc["mcp_servers"]["other"]["command"].as_str(), Some("/bin/other"));
        assert_eq!(notes, vec!["Codex receipt keeps its custom approval policy"]);
    }

    #[test]
    fn only_target_listing_is_advertised_as_read_only() {
        let listed = tools();
        for tool in listed["tools"].as_array().unwrap() {
            assert_eq!(tool["annotations"]["readOnlyHint"], tool["name"] == "list_targets");
            assert_eq!(tool["annotations"]["destructiveHint"], tool["name"] == "send");
            assert_eq!(tool["annotations"]["openWorldHint"], false);
        }
    }

    #[test]
    fn tool_arguments_never_accept_a_model_supplied_actor() {
        assert!(allowed(
            "send",
            &json!({"destination":"session:s","text":"hello","requestId":"r"})
        ));
        assert!(!allowed(
            "send",
            &json!({"destination":"session:s","text":"hello","requestId":"r","sourceSessionId":"other"})
        ));
        assert!(!allowed(
            "receipt",
            &json!({"requestId":"r","actor":"owner"})
        ));
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
