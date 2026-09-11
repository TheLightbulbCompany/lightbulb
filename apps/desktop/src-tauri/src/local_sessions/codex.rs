use super::*;

fn from_rollout(
    root: &Path,
    process: Process,
    path: &Path,
    version: &str,
) -> Result<NativeSession> {
    let path = confined_path(root, path)?;
    let mut reader = BufReader::new(secure_file(&path)?.take(65536));
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .map_err(|_| "rollout unavailable")?;
    let row: Value = serde_json::from_str(&line).map_err(|_| "rollout metadata unavailable")?;
    if row["type"] != "session_meta" || row["payload"]["source"] != "cli" {
        return Err("not an ordinary CLI rollout".into());
    }
    let conversation = row["payload"]["id"]
        .as_str()
        .filter(|s| !s.is_empty() && s.len() <= 128)
        .ok_or("invalid native identity")?;
    let reported = row["payload"]["cli_version"]
        .as_str()
        .ok_or("missing runtime version")?;
    Ok(NativeSession {
        provider: "codex".into(),
        conversation: conversation.into(),
        profile: fingerprint(&root.to_string_lossy()),
        process,
        transcript: Some(path),
        socket: None,
        socket_inode: None,
        version: reported.into(),
        restriction: (reported != "0.149.0" || reported != version)
            .then(|| "runtime-unverified".into()),
        status: "unknown".into(),
    })
}

pub(super) async fn capture(home: &Path, program: &Path, pid: u32) -> Result<NativeSession> {
    let execution = process(pid)?;
    let root = home.join(".codex/sessions");
    confined_path(home, &root)?;
    let paths: Vec<_> = open_files(pid)
        .await?
        .into_iter()
        .filter(|p| {
            p.starts_with(&root)
                && p.file_name()
                    .is_some_and(|n| n.to_string_lossy().starts_with("rollout-"))
                && p.extension().is_some_and(|e| e == "jsonl")
        })
        .collect();
    if paths.len() != 1 {
        return Err("native rollout is not available yet".into());
    }
    let version = runtime_version(&execution, program, "codex")
        .await
        .unwrap_or_default();
    let found = from_rollout(&root, execution.clone(), &paths[0], &version)?;
    if process(pid)? != execution {
        return Err("process changed during discovery".into());
    }
    Ok(found)
}

pub(super) async fn discover(home: &Path) -> Result<Vec<NativeSession>> {
    let Some(program) = provider_program(home, "codex") else {
        return Ok(vec![]);
    };
    let processes = native_output(Path::new("/bin/ps"), &["-axo", "pid=,comm="]).await?;
    let mut found = vec![];
    for row in processes.lines() {
        let mut fields = row.trim().splitn(2, char::is_whitespace);
        let Some(pid) = fields.next().and_then(|p| p.parse().ok()) else {
            continue;
        };
        let Some(name) = fields.next() else {
            continue;
        };
        if !name.contains("/codex") {
            continue;
        }
        if let Ok(session) = capture(home, &program, pid).await {
            found.push(session);
        }
    }
    Ok(found)
}

fn queued_id(output: &str, thread: &str) -> Option<String> {
    let (id, target) = output
        .trim()
        .strip_prefix("Queued message ")?
        .strip_suffix('.')?
        .split_once(" for thread ")?;
    (target == thread && uuid::Uuid::parse_str(id).is_ok()).then(|| id.into())
}

pub(super) async fn send(native: &NativeSession, text: &str) -> Result<Option<String>> {
    let output = native_output(
        &native.process.executable,
        &["queue", "--thread", &native.conversation, "--message", text],
    )
    .await?;
    if process(native.process.pid)? != native.process {
        return Ok(None);
    }
    Ok(queued_id(&output, &native.conversation))
}

pub(super) fn normalize(session: &str, row: &Value) -> Result<Option<Value>> {
    if row.get("channel").is_some() || row.get("phase").is_some() {
        return Ok(None);
    }
    if row["type"] == "session_meta"
        && (row["payload"]["id"] != session || row["payload"]["cli_version"] != "0.149.0")
    {
        return Err("runtime-unverified".into());
    }
    let body = &row["payload"];
    if row["type"] != "response_item"
        || body["type"] != "message"
        || body.get("channel").is_some()
        || match body["role"].as_str() {
            Some("assistant") => {
                !matches!(body["phase"].as_str(), Some("commentary" | "final_answer"))
            }
            Some("user") => body.get("phase").is_some(),
            _ => true,
        }
    {
        return Ok(None);
    }
    let text = body["content"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|b| {
            b["type"]
                == if body["role"] == "user" {
                    "input_text"
                } else {
                    "output_text"
                }
        })
        .filter_map(|b| b["text"].as_str())
        .collect::<Vec<_>>()
        .join("\n");
    Ok((!text.is_empty())
        .then(|| json!({"kind":"text","role":body["role"],"text":text,"truncated":false})))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_sessions_codex_public_phases_only() {
        let meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.149.0","source":"cli"}});
        assert!(normalize("s", &meta).unwrap().is_none());
        assert!(normalize("other", &meta).is_err());
        let row = json!({"type":"response_item","timestamp":"2026-09-11T00:00:00Z","payload":{
            "type":"message","role":"assistant","phase":"final_answer","id":"m","content":[{"type":"output_text","text":"done"}]}});
        assert_eq!(normalize("s", &row).unwrap().unwrap()["text"], "done");
        for phase in ["analysis", "unknown"] {
            let mut unknown = row.clone();
            unknown["payload"]["phase"] = json!(phase);
            assert!(normalize("s", &unknown).unwrap().is_none());
        }
    }
    #[test]
    fn local_sessions_codex_discovers_only_process_owned_cli_profile_history() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-owned.jsonl");
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        let process = Process {
            pid: 42,
            birth_micros: 123,
            executable: "/bin/codex".into(),
        };
        let found = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(found.conversation, "owned");
        assert_eq!(found.process.birth_micros, 123);
        assert_eq!(found.status, "unknown");
        assert!(from_rollout(
            &root.join("other-profile"),
            process.clone(),
            &path,
            "0.149.0"
        )
        .is_err());
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":\"exec\",\"cli_version\":\"0.149.0\"}}\n").unwrap();
        assert!(from_rollout(&root, process, &path, "0.149.0").is_err());
    }
    #[tokio::test]
    #[ignore = "requires an explicitly owned disposable native Codex PID and conversation ID"]
    async fn local_sessions_owned_codex_capture_binds_the_running_binary() {
        let home = PathBuf::from(std::env::var("HOME").unwrap());
        let pid = std::env::var("LIGHTBULB_TEST_CODEX_PID")
            .unwrap()
            .parse()
            .unwrap();
        let conversation = std::env::var("LIGHTBULB_TEST_CODEX_THREAD").unwrap();
        let program = provider_program(&home, "codex").unwrap();
        let session = capture(&home, &program, pid).await.unwrap();
        assert_eq!(session.conversation, conversation);
        assert_eq!(session.restriction, None);
        assert_eq!(session.version, "0.149.0");
    }

    #[test]
    fn local_sessions_codex_receipt_is_exact_and_thread_bound() {
        let observed = "Queued message 01a08ec5-b961-77c0-962d-25097fe0f5f4 for thread 01a08ec5-554f-7dc1-baf4-5755bb2de4d1.\n";
        assert_eq!(
            queued_id(observed, "01a08ec5-554f-7dc1-baf4-5755bb2de4d1").as_deref(),
            Some("01a08ec5-b961-77c0-962d-25097fe0f5f4")
        );
        assert!(queued_id(observed, "other").is_none());
        assert!(queued_id(
            &format!("unknown {observed}"),
            "01a08ec5-554f-7dc1-baf4-5755bb2de4d1"
        )
        .is_none());
    }
}
