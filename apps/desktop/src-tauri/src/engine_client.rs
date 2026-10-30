//! The network engine (`lightbulb`) over its socket: one newline-JSON request, one response line.
//! `ask` is for native code and unfiltered. A page is a remote origin, so its `engine_ask` is
//! read-only: only `ALLOWED` passes and `for_page` cuts the answer. Joining is native (Step 2),
//! never from a page-supplied ticket. Nothing here logs a body (`team.join` carries a ticket).
// ponytail: no `subscribe`; polling `directory` works. Add a held connection when it does not.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

const ALLOWED: [&str; 3] = ["directory", "sessions", "team.get"];
/// Off every session record the page gets: this Mac's paths and process details.
const NATIVE_ONLY: [&str; 5] = ["pid", "birth_micros", "entrypoint", "kind", "cwd"];
const LIMIT: Duration = Duration::from_secs(5);

/// `$LIGHTBULB_CONNECT_HOME/sock`, else `~/.lightbulb/connect/sock`: the engine's `paths.rs`.
fn socket() -> Result<PathBuf, String> {
    std::env::var_os("LIGHTBULB_CONNECT_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".lightbulb/connect")))
        .map(|dir| dir.join("sock"))
        .ok_or_else(|| "no home directory".to_owned())
}

/// `{id, op, ...body}`. `op` goes in last: a body naming its own op must not change it.
fn frame(op: &str, body: Option<Value>) -> Result<String, String> {
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
/// no answer came. Native callers only: no allowlist, nothing cut.
#[allow(dead_code)] // Steps 2 and 3 call it.
pub async fn ask(op: &str, body: Option<Value>) -> Result<Value, String> {
    ask_at(&socket()?, op, body, LIMIT).await
}

/// What a page may see of an answer. A record with no `folder` gets its `cwd`'s last part first.
fn for_page(op: &str, mut answer: Value) -> Value {
    if op == "team.get" {
        let members: Vec<Value> = answer["members"].as_array().into_iter().flatten()
            .map(|m| json!({"name": m["name"], "computer": m["computer"]}))
            .collect();
        let mut kept = Map::new();
        // The envelope stays: a refusal has to read as one.
        for key in ["id", "ok", "error", "team_id", "name"] {
            if let Some(value) = answer.get(key) {
                kept.insert(key.to_owned(), value.clone());
            }
        }
        if answer["ok"] == true {
            kept.insert("members".to_owned(), members.into());
        }
        return Value::Object(kept);
    }
    for record in answer["sessions"].as_array_mut().into_iter().flatten().filter_map(Value::as_object_mut) {
        let from_cwd = record.get("cwd").and_then(Value::as_str).and_then(|cwd| Path::new(cwd).file_name()?.to_str()).map(str::to_owned);
        if record.get("folder").and_then(Value::as_str).unwrap_or("").is_empty() {
            if let Some(folder) = from_cwd {
                record.insert("folder".to_owned(), folder.into());
            }
        }
        for field in NATIVE_ONLY {
            record.remove(field);
        }
    }
    answer
}

async fn page_ask_at(path: &Path, op: &str, body: Option<Value>, limit: Duration) -> Result<Value, String> {
    if !ALLOWED.contains(&op) {
        return Err(format!("engine op not allowed: {op}"));
    }
    Ok(for_page(op, ask_at(path, op, body, limit).await?))
}

/// The page's read-only view of the engine.
#[tauri::command]
pub async fn engine_ask(op: String, body: Option<Value>) -> Result<Value, String> {
    page_ask_at(&socket()?, &op, body, LIMIT).await
}

#[cfg(test)]
mod engine_client_tests {
    use super::*;
    use tokio::net::UnixListener;

    /// A stub engine: answers each connection's one line with `{id, ok, seen: <the request>}` and `extra`.
    fn stub(dir: &tempfile::TempDir, extra: Value) -> PathBuf {
        let path = dir.path().join("sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                let mut stream = BufReader::new(stream);
                let mut line = String::new();
                stream.read_line(&mut line).await.unwrap();
                let seen: Value = serde_json::from_str(&line).unwrap();
                let mut answer = json!({"id": seen["id"], "ok": true, "seen": seen});
                answer.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
                stream.get_mut().write_all(format!("{answer}\n").as_bytes()).await.unwrap();
            }
        });
        path
    }

    #[tokio::test]
    async fn one_request_one_answer_with_the_body_flat() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir, json!({}));
        let answer = page_ask_at(&path, "directory", Some(json!({"query": "x"})), LIMIT).await.unwrap();
        assert_eq!(answer["ok"], true);
        assert_eq!(answer["seen"], json!({"id": 1, "op": "directory", "query": "x"}));
        assert_eq!(page_ask_at(&path, "sessions", None, LIMIT).await.unwrap()["seen"], json!({"id": 1, "op": "sessions"}));
    }

    #[tokio::test]
    async fn refuses_every_op_off_the_list_before_the_socket() {
        assert_eq!(ALLOWED, ["directory", "sessions", "team.get"]);
        let missing = Path::new("/nonexistent/sock");
        for op in ["", "team.join", "team.leave", "approve", "admit", "policy", "team.invite", "team.allow", "subscribe", "inbox", "shutdown"] {
            let refused = page_ask_at(missing, op, None, LIMIT).await.unwrap_err();
            assert!(refused.contains("not allowed"), "{op}: {refused}");
        }
    }

    #[tokio::test]
    async fn a_body_cannot_name_its_own_op_or_be_a_non_object() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir, json!({}));
        let answer = page_ask_at(&path, "directory", Some(json!({"op": "team.join", "id": 9})), LIMIT).await.unwrap();
        assert_eq!(answer["seen"], json!({"id": 1, "op": "directory"}));
        assert!(page_ask_at(&path, "directory", Some(json!("team.join")), LIMIT).await.is_err());
    }

    #[tokio::test]
    async fn no_answer_times_out_and_no_engine_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sock");
        assert!(page_ask_at(&path, "directory", None, LIMIT).await.unwrap_err().contains("unreachable"));
        let _silent = UnixListener::bind(&path).unwrap();
        let late = page_ask_at(&path, "directory", None, Duration::from_millis(50)).await.unwrap_err();
        assert!(late.contains("in time"), "{late}");
    }

    #[tokio::test]
    async fn the_page_gets_records_without_native_fields_and_native_gets_them_all() {
        let dir = tempfile::tempdir().unwrap();
        let live = json!({"session_id": "a", "folder": "repo", "cwd": "/Users/me/repo", "pid": 1, "birth_micros": 2, "entrypoint": "cli", "kind": "interactive", "resume": "claude -r a", "job": {"state": "done"}});
        let ended = json!({"key": "b", "cwd": "/Users/me/other/", "resume": "claude -r b"});
        let path = stub(&dir, json!({"sessions": [live, ended]}));
        for op in ["directory", "sessions"] {
            let answer = page_ask_at(&path, op, None, LIMIT).await.unwrap();
            assert_eq!(answer["sessions"], json!([
                {"session_id": "a", "folder": "repo", "resume": "claude -r a", "job": {"state": "done"}},
                {"key": "b", "folder": "other", "resume": "claude -r b"},
            ]));
        }
        let native = ask_at(&path, "directory", None, LIMIT).await.unwrap();
        assert_eq!(native["sessions"][0]["pid"], 1);
        assert_eq!(native["sessions"][0]["cwd"], "/Users/me/repo");
    }

    #[tokio::test]
    async fn team_get_keeps_only_its_id_name_and_members_names_and_computers() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir, json!({"team_id": "t", "name": "Acme", "heads": ["h"], "removed": null,
            "members": [{"key": "secret", "short": "s", "name": "Leo", "computer": "mac", "person_id": "p", "role": "member", "online": true}]}));
        let answer = page_ask_at(&path, "team.get", None, LIMIT).await.unwrap();
        assert_eq!(answer, json!({"id": 1, "ok": true, "team_id": "t", "name": "Acme", "members": [{"name": "Leo", "computer": "mac"}]}));
        assert_eq!(for_page("team.get", json!({"id": 1, "ok": false, "error": "e", "heads": []})), json!({"id": 1, "ok": false, "error": "e"}));
        assert_eq!(ask_at(&path, "team.get", None, LIMIT).await.unwrap()["heads"], json!(["h"]));
    }
}
