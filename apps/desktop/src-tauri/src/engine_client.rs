//! The page's one door to the network engine (`lightbulb`): one newline-JSON request on the
//! engine's socket, one response line. A page is a remote origin, so only `ALLOWED` passes, and
//! nothing here logs a body (`team.join` carries a ticket).
// ponytail: no `subscribe`; polling `directory` works. Add a held connection when it does not.

use serde_json::{Map, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

const ALLOWED: [&str; 5] = ["directory", "sessions", "team.get", "team.join", "team.leave"];
const LIMIT: Duration = Duration::from_secs(5);

/// `$LIGHTBULB_CONNECT_HOME/sock`, else `~/.lightbulb/connect/sock`: the engine's `paths.rs`.
fn socket() -> Result<PathBuf, String> {
    std::env::var_os("LIGHTBULB_CONNECT_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".lightbulb/connect")))
        .map(|dir| dir.join("sock"))
        .ok_or_else(|| "no home directory".to_owned())
}

/// `{id, op, ...body}`. `op` goes in last: a body naming its own op must not pass the allowlist.
fn frame(op: &str, body: Option<Value>) -> Result<String, String> {
    if !ALLOWED.contains(&op) {
        return Err(format!("engine op not allowed: {op}"));
    }
    let mut frame = match body {
        None | Some(Value::Null) => Map::new(),
        Some(Value::Object(body)) => body,
        Some(_) => return Err("engine body must be an object".to_owned()),
    };
    frame.insert("id".to_owned(), 1.into());
    frame.insert("op".to_owned(), op.into());
    Ok(Value::Object(frame).to_string())
}

async fn exchange(path: &Path, frame: &str) -> Result<Value, String> {
    let mut stream = UnixStream::connect(path).await.map_err(|e| format!("engine unreachable: {e}"))?;
    stream.write_all(format!("{frame}\n").as_bytes()).await.map_err(|e| format!("engine write: {e}"))?;
    let mut line = String::new();
    let read = BufReader::new(stream).read_line(&mut line).await.map_err(|e| format!("engine read: {e}"))?;
    if read == 0 {
        return Err("engine closed without answering".to_owned());
    }
    serde_json::from_str(&line).map_err(|_| "engine answered something unreadable".to_owned())
}

async fn ask_at(path: &Path, op: &str, body: Option<Value>, limit: Duration) -> Result<Value, String> {
    let frame = frame(op, body)?;
    tokio::time::timeout(limit, exchange(path, &frame)).await.map_err(|_| "engine did not answer in time".to_owned())?
}

/// The engine's response line as it is (`{id, ok, ...}`: a refusal is `ok: false`). `Err` is that
/// no answer came, or that the op is not allowed.
#[tauri::command]
pub async fn engine_ask(op: String, body: Option<Value>) -> Result<Value, String> {
    ask_at(&socket()?, &op, body, LIMIT).await
}

#[cfg(test)]
mod engine_client_tests {
    use super::*;
    use serde_json::json;
    use tokio::net::UnixListener;

    /// A stub engine: answers each connection's one line with `{id, ok, seen: <the request>}`.
    fn stub(dir: &tempfile::TempDir) -> PathBuf {
        let path = dir.path().join("sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let mut stream = BufReader::new(stream);
                let mut line = String::new();
                stream.read_line(&mut line).await.unwrap();
                let seen: Value = serde_json::from_str(&line).unwrap();
                let answer = json!({"id": seen["id"], "ok": true, "seen": seen});
                stream.get_mut().write_all(format!("{answer}\n").as_bytes()).await.unwrap();
            }
        });
        path
    }

    #[tokio::test]
    async fn one_request_one_answer_with_the_body_flat() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir);
        let answer = ask_at(&path, "team.join", Some(json!({"line": "x"})), LIMIT).await.unwrap();
        assert_eq!(answer["ok"], true);
        assert_eq!(answer["seen"], json!({"id": 1, "op": "team.join", "line": "x"}));
        assert_eq!(ask_at(&path, "directory", None, LIMIT).await.unwrap()["seen"], json!({"id": 1, "op": "directory"}));
    }

    #[tokio::test]
    async fn refuses_every_op_off_the_list_before_the_socket() {
        assert_eq!(ALLOWED, ["directory", "sessions", "team.get", "team.join", "team.leave"]);
        let missing = Path::new("/nonexistent/sock");
        for op in ["", "approve", "admit", "policy", "team.invite", "team.allow", "subscribe", "inbox", "shutdown"] {
            let refused = ask_at(missing, op, None, LIMIT).await.unwrap_err();
            assert!(refused.contains("not allowed"), "{op}: {refused}");
        }
    }

    #[tokio::test]
    async fn a_body_cannot_name_its_own_op_or_be_a_non_object() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir);
        let answer = ask_at(&path, "directory", Some(json!({"op": "approve", "id": 9})), LIMIT).await.unwrap();
        assert_eq!(answer["seen"], json!({"id": 1, "op": "directory"}));
        assert!(ask_at(&path, "directory", Some(json!("approve")), LIMIT).await.is_err());
    }

    #[tokio::test]
    async fn no_answer_times_out_and_no_engine_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sock");
        assert!(ask_at(&path, "directory", None, LIMIT).await.unwrap_err().contains("unreachable"));
        let _silent = UnixListener::bind(&path).unwrap();
        let late = ask_at(&path, "directory", None, Duration::from_millis(50)).await.unwrap_err();
        assert!(late.contains("in time"), "{late}");
    }
}
