//! The network engine (`lightbulb`) over its socket: one newline-JSON request, one response line.
//! `ask` is for native code and unfiltered. A page is a remote origin, so its `engine_ask` is
//! read-only: only `ALLOWED` passes, it takes no body (a `directory` query would be an oracle for
//! paths it never sees), and `for_page` rebuilds the answer from fields named here. Joining is
//! native (Step 2), never from a page-supplied ticket. Nothing here logs a body (`team.join`
//! carries a ticket).
// ponytail: no `subscribe`; polling `directory` works. Add a held connection when it does not.

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;

const ALLOWED: [&str; 3] = ["directory", "sessions", "team.get"];
/// All a session record shows a page. Never `cwd`, nor `resume`, which holds it (`cd '<cwd>' && ...`).
const RECORD_FIELDS: [&str; 26] = [
    "session_id", "key", "provider", "conversation", "handle", "person", "person_id", "person_verified",
    "computer", "agent", "folder", "repo", "branch", "status", "waiting_for", "doing", "title", "tier",
    "online", "seen_at", "wakes", "user_id", "ended_at", "reason", "hidden", "job",
];
const RECORD_LISTS: [&str; 2] = ["sessions", "ended"];
const LIMIT: Duration = Duration::from_secs(5);

/// `$LIGHTBULB_CONNECT_HOME`, else `~/.lightbulb/connect`: the engine's `paths.rs`.
pub(crate) fn state_dir() -> Result<PathBuf, String> {
    std::env::var_os("LIGHTBULB_CONNECT_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".lightbulb/connect")))
        .ok_or_else(|| "no home directory".to_owned())
}

fn socket() -> Result<PathBuf, String> {
    Ok(state_dir()?.join("sock"))
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

pub(crate) async fn ask_at(path: &Path, op: &str, body: Option<Value>, limit: Duration) -> Result<Value, String> {
    let frame = frame(op, body)?;
    tokio::time::timeout(limit, exchange(path, &frame)).await.map_err(|_| "engine did not answer in time".to_owned())?
}

/// The engine's response line as it is (`{id, ok, ...}`: a refusal is `ok: false`). `Err` is that
/// no answer came. Native callers only: no allowlist, nothing cut.
pub async fn ask(op: &str, body: Option<Value>) -> Result<Value, String> {
    ask_at(&socket()?, op, body, LIMIT).await
}

/// The named fields of `from`, where it is an object and has them.
fn pick(from: &Value, fields: &[&str]) -> Map<String, Value> {
    fields.iter().filter_map(|&field| Some((field.to_owned(), from.get(field)?.clone()))).collect()
}

/// A record with no `folder` gets its `cwd`'s last part. `can_resume` says a `resume` exists; `own`
/// that the record is this computer's (only local records carry a `pid` or a `resume` key).
fn page_record(record: &Value) -> Option<Value> {
    record.as_object()?;
    let mut kept = pick(record, &RECORD_FIELDS);
    if kept.get("folder").and_then(Value::as_str).unwrap_or("").is_empty() {
        let cwd = record.get("cwd").and_then(Value::as_str).unwrap_or("");
        if let Some(folder) = Path::new(cwd).file_name().and_then(|name| name.to_str()) {
            kept.insert("folder".to_owned(), folder.into());
        }
    }
    if record.get("resume").and_then(Value::as_str).is_some_and(|resume| !resume.is_empty()) {
        kept.insert("can_resume".to_owned(), true.into());
    }
    // The engine marks no record as own; only a local record (`Session`, `store::Session`) has a `pid` or `resume` key.
    if record.get("pid").is_some() || record.get("resume").is_some() {
        kept.insert("own".to_owned(), true.into());
    }
    Some(Value::Object(kept))
}

/// What a page may see of an answer: its envelope (a refusal has to read as one) and the named
/// fields. Anything else, or a line that is not an object, does not reach it.
fn for_page(op: &str, answer: &Value) -> Value {
    let mut kept = pick(answer, &["id", "ok", "error"]);
    let list = |key: &str| answer.get(key).and_then(Value::as_array).map(Vec::as_slice);
    if op == "team.get" {
        kept.extend(pick(answer, &["team_id", "name"]));
        if let Some(members) = list("members") {
            let members = members.iter().map(|member| Value::Object(pick(member, &["name", "computer"]))).collect();
            kept.insert("members".to_owned(), Value::Array(members));
        }
    } else {
        for key in RECORD_LISTS {
            if let Some(records) = list(key) {
                kept.insert(key.to_owned(), records.iter().filter_map(page_record).collect());
            }
        }
    }
    Value::Object(kept)
}

async fn page_ask_at(path: &Path, op: &str, limit: Duration) -> Result<Value, String> {
    if !ALLOWED.contains(&op) {
        return Err(format!("engine op not allowed: {op}"));
    }
    Ok(for_page(op, &ask_at(path, op, None, limit).await?))
}

/// The page's read-only view of the engine.
#[tauri::command]
pub async fn engine_ask(op: String) -> Result<Value, String> {
    page_ask_at(&socket()?, &op, LIMIT).await
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
    async fn native_sends_one_request_with_the_body_flat_and_its_own_op() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir, json!({}));
        let answer = ask_at(&path, "team.join", Some(json!({"line": "x"})), LIMIT).await.unwrap();
        assert_eq!(answer["seen"], json!({"id": 1, "op": "team.join", "line": "x"}));
        let answer = ask_at(&path, "directory", Some(json!({"op": "team.join", "id": 9})), LIMIT).await.unwrap();
        assert_eq!(answer["seen"], json!({"id": 1, "op": "directory"}));
        assert!(ask_at(&path, "directory", Some(json!("team.join")), LIMIT).await.is_err());
    }

    #[tokio::test]
    async fn refuses_every_op_off_the_list_before_the_socket() {
        assert_eq!(ALLOWED, ["directory", "sessions", "team.get"]);
        let missing = Path::new("/nonexistent/sock");
        for op in ["", "team.join", "team.leave", "approve", "admit", "policy", "team.invite", "team.allow", "subscribe", "inbox", "shutdown"] {
            let refused = page_ask_at(missing, op, LIMIT).await.unwrap_err();
            assert!(refused.contains("not allowed"), "{op}: {refused}");
        }
    }

    #[tokio::test]
    async fn no_answer_times_out_and_no_engine_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sock");
        assert!(page_ask_at(&path, "directory", LIMIT).await.unwrap_err().contains("unreachable"));
        let _silent = UnixListener::bind(&path).unwrap();
        let late = page_ask_at(&path, "directory", Duration::from_millis(50)).await.unwrap_err();
        assert!(late.contains("in time"), "{late}");
    }

    #[tokio::test]
    async fn the_page_gets_named_fields_in_every_list_and_native_gets_them_all() {
        let dir = tempfile::tempdir().unwrap();
        let resume = "cd '/Users/me/other' && claude --resume b";
        let live = json!({"session_id": "a", "folder": "repo", "cwd": "/Users/me/repo", "pid": 1, "birth_micros": 2, "entrypoint": "cli", "kind": "interactive", "transcript": "/Users/me/t", "job": {"state": "done"}});
        let ended = json!({"key": "b", "cwd": "/Users/me/other/", "resume": resume});
        let no_resume = json!({"key": "c", "cwd": "/Users/me/third", "resume": null});
        let teammate = json!({"session_id": "t", "folder": "x", "cwd": "/c"});
        let path = stub(&dir, json!({"sessions": [live, 7, teammate], "ended": [ended, no_resume], "secret": "/Users/me"}));
        for op in ["directory", "sessions"] {
            let answer = page_ask_at(&path, op, LIMIT).await.unwrap();
            assert_eq!(answer, json!({"id": 1, "ok": true,
                "sessions": [{"session_id": "a", "folder": "repo", "job": {"state": "done"}, "own": true}, {"session_id": "t", "folder": "x"}],
                "ended": [{"key": "b", "folder": "other", "can_resume": true, "own": true}, {"key": "c", "folder": "third", "own": true}],
            }));
            assert!(!answer.to_string().contains("/Users"));
        }
        let native = ask_at(&path, "directory", None, LIMIT).await.unwrap();
        assert_eq!(native["sessions"][0]["cwd"], "/Users/me/repo");
        assert_eq!(native["ended"][0]["resume"], resume);
    }

    #[tokio::test]
    async fn team_get_keeps_only_its_id_name_and_members_names_and_computers() {
        let dir = tempfile::tempdir().unwrap();
        let path = stub(&dir, json!({"team_id": "t", "name": "Acme", "heads": ["h"], "removed": null,
            "members": [{"key": "secret", "short": "s", "name": "Leo", "computer": "mac", "person_id": "p", "role": "member", "online": true}]}));
        let answer = page_ask_at(&path, "team.get", LIMIT).await.unwrap();
        assert_eq!(answer, json!({"id": 1, "ok": true, "team_id": "t", "name": "Acme", "members": [{"name": "Leo", "computer": "mac"}]}));
        assert_eq!(for_page("team.get", &json!({"id": 1, "ok": false, "error": "e", "heads": []})), json!({"id": 1, "ok": false, "error": "e"}));
        assert_eq!(ask_at(&path, "team.get", None, LIMIT).await.unwrap()["heads"], json!(["h"]));
    }

    #[test]
    fn a_line_that_is_not_an_object_reaches_nothing_and_does_not_panic() {
        for op in ALLOWED {
            for line in [json!(null), json!(1), json!("x"), json!([1]), json!({"sessions": "x", "members": 1})] {
                assert_eq!(for_page(op, &line), json!({}), "{op} {line}");
            }
        }
    }
}
