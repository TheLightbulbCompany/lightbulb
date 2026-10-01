//! The native CLI's small, session-bound Lightbulb MCP server.
//!
//! The model chooses only a destination and text. The running Mac app derives
//! the source from OS peer identity and provider-owned MCP metadata; it refuses
//! a call when that source cannot be proved against a live mirrored session.

use serde_json::{json, Value};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
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
                }
            }
            Ok(Some(_)) => notes.push(format!("{provider} has a different {SERVER_NAME} server")),
            Ok(None) if enabled => {
                if change(home, &cli, provider, "add", executable).is_err() {
                    notes.push(format!("{provider} native tools could not be installed"));
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
        {"name":"send","description":"Send one Lightbulb message to an accessible room, person or shared session. Use a stable requestId for retries. Cannot answer permission prompts.",
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
    socket.set_read_timeout(Some(Duration::from_secs(10)))?;
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
                    Ok(reply) => json!({"content":[{"type":"text","text":reply.to_string()}],
                        "isError":reply["ok"] != true}),
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
    fn only_target_listing_is_advertised_as_read_only() {
        let listed = tools();
        let tools = listed["tools"].as_array().unwrap();
        for tool in tools {
            let read_only = tool["name"] == "list_targets";
            assert_eq!(tool["annotations"]["readOnlyHint"], read_only);
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
