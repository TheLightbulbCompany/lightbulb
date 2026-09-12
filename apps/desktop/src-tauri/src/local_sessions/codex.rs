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
    let (status, active_turn, title) = tail_state(&path);
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
        status: status.into(),
        title,
        // The backend's SessionRegistration caps cwd_name at 128 characters.
        cwd_name: row["payload"]["cwd"]
            .as_str()
            .and_then(|c| Path::new(c).file_name())
            .map(|n| n.to_string_lossy().chars().take(128).collect()),
        active_turn,
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

/// Last turn event and first prompt in the rollout's final 2 MiB.
pub(super) fn tail_state(path: &Path) -> (&'static str, Option<String>, Option<String>) {
    let Ok(mut file) = secure_file(path) else {
        return ("unknown", None, None);
    };
    let Ok(len) = file.metadata().map(|m| m.len()) else {
        return ("unknown", None, None);
    };
    let offset = len.saturating_sub(2 * 1024 * 1024);
    if file.seek(SeekFrom::Start(offset)).is_err() {
        return ("unknown", None, None);
    }
    let mut reader = BufReader::new(file.take(2 * 1024 * 1024));
    if offset > 0 {
        let mut partial = String::new();
        let _ = reader.read_line(&mut partial);
    }
    let (mut status, mut turn, mut title) = ("unknown", None, None);
    for line in reader.lines().map_while(|l| l.ok()) {
        let Ok(row) = serde_json::from_str::<Value>(&line) else {
            continue;
        };
        if row["type"] != "event_msg" {
            continue;
        }
        let payload = &row["payload"];
        match payload["type"].as_str() {
            Some("task_started") => {
                status = "busy";
                turn = payload["turn_id"].as_str().map(str::to_owned);
            }
            Some("task_complete") | Some("turn_aborted") => {
                status = "idle";
                turn = None;
            }
            Some("item_completed")
                if title.is_none() && payload["item"]["type"] == "UserMessage" =>
            {
                let text = payload["item"]["content"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|b| b["type"] == "text")
                    .filter_map(|b| b["text"].as_str())
                    .collect::<Vec<_>>()
                    .join(" ");
                let first = text.lines().next().unwrap_or("").trim();
                if !first.is_empty() && !first.starts_with(HEADER_PREFIX) {
                    title = Some(redact(first).chars().take(60).collect());
                }
            }
            _ => {}
        }
    }
    (status, turn, title)
}

fn item(item: &Value, ts: Option<&str>) -> Option<Value> {
    let id = item["id"].as_str();
    let texts = |content: &Value, block: &str| {
        content
            .as_array()
            .into_iter()
            .flatten()
            .filter(|b| b["type"] == block)
            .filter_map(|b| b["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n")
    };
    let basename = |p: &str| {
        Path::new(p)
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    Some(match item["type"].as_str()? {
        "UserMessage" => {
            let text = texts(&item["content"], "text");
            if text.is_empty() {
                return None;
            }
            match teammate_contribution(&text) {
                Some((provenance, body)) => {
                    let mut e = event("text", "user", &body, ts, id);
                    if let Some(actor) = actor_of(&provenance) {
                        e["actor"] = actor;
                    }
                    e
                }
                None => event("text", "user", &text, ts, id),
            }
        }
        "AgentMessage" => {
            // The TUI renders every AgentMessage, and the phase is often absent; withhold only a
            // phase this build does not know, which may not be transcript prose at all.
            if item["phase"]
                .as_str()
                .is_some_and(|p| p != "commentary" && p != "final_answer")
            {
                return None;
            }
            let text = texts(&item["content"], "Text");
            if text.is_empty() {
                return None;
            }
            event("text", "assistant", &text, ts, id)
        }
        "Reasoning" => {
            let text = item["summary_text"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("\n");
            event("thinking", "assistant", &text, ts, id)
        }
        "CommandExecution" => {
            let command = item["command"]
                .as_array()
                .and_then(|c| c.last())
                .and_then(Value::as_str)
                .unwrap_or("");
            let output = item["aggregated_output"].as_str().unwrap_or("");
            let (text, lines) = preview(output, PREVIEW_LINES);
            let failed =
                item["status"] == "failed" || item["exit_code"].as_i64().is_some_and(|c| c != 0);
            let mut e = event("tool", "assistant", &text, ts, id);
            let (input, input_cut) = bound_json(json!({"command": command}), MAX_TOOL_INPUT);
            let (result, result_cut) = bound_json(
                json!({"exitCode": item["exit_code"], "output": output}),
                MAX_TOOL_RESULT,
            );
            e["tool"] = json!({"name": "CommandExecution",
                "status": if item["status"] == "in_progress" { "running" } else if failed { "error" } else { "done" },
                "input": input, "result": result, "lines": lines});
            if input_cut || result_cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "FileChange" => {
            let changes: serde_json::Map<String, Value> = item["changes"].as_object().into_iter().flatten()
                .map(|(path, change)| (basename(path), json!({"type": change["type"], "diff": change["diff"], "content": change["content"]})))
                .collect();
            let names = changes.keys().cloned().collect::<Vec<_>>().join(", ");
            let mut e = event("tool", "assistant", &names, ts, id);
            let (result, cut) = bound_json(Value::Object(changes), MAX_TOOL_RESULT);
            e["tool"] = json!({"name": "FileChange", "status": if item["status"] == "failed" { "error" } else { "done" },
                "result": result});
            if cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "McpToolCall" => {
            let label = format!(
                "{}.{}",
                item["server"].as_str().unwrap_or(""),
                item["tool"].as_str().unwrap_or("")
            );
            let mut e = event("tool", "assistant", &label, ts, id);
            let (input, input_cut) = bound_json(
                json!({"server": item["server"], "tool": item["tool"], "arguments": item["arguments"]}),
                MAX_TOOL_INPUT,
            );
            let (result, result_cut) = bound_json(
                json!({"content": item["result"]["content"]}),
                MAX_TOOL_RESULT,
            );
            e["tool"] = json!({"name": "McpToolCall",
                "status": if item["result"]["isError"] == true || item["status"] == "failed" { "error" } else { "done" },
                "input": input, "result": result});
            if input_cut || result_cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "Extension" => {
            let query = item["query"].as_str().unwrap_or("");
            let mut e = event("tool", "assistant", query, ts, id);
            let (input, cut) = bound_json(json!({"query": query}), MAX_TOOL_INPUT);
            e["tool"] = json!({"name": item["kind"].as_str().unwrap_or("Extension"), "status": "done", "input": input});
            if cut {
                e["truncated"] = json!(true);
            }
            e
        }
        "SubAgentActivity" | "CollabAgentToolCall" => {
            let label = item["agent_path"]
                .as_str()
                .or(item["tool"].as_str())
                .unwrap_or("");
            let mut e = event("tool", "assistant", label, ts, id);
            e["tool"] = json!({"name": item["type"], "status": "done"});
            e
        }
        "ContextCompaction" => lifecycle("compaction", "", ts, id),
        _ => return None,
    })
}

pub(super) fn normalize(session: &str, row: &Value) -> Result<Vec<Value>> {
    if row.get("channel").is_some() || row.get("phase").is_some() {
        return Ok(vec![]);
    }
    if row["type"] == "session_meta"
        && (row["payload"]["id"] != session || row["payload"]["cli_version"] != "0.149.0")
    {
        return Err("runtime-unverified".into());
    }
    if row["type"] != "event_msg" {
        return Ok(vec![]);
    }
    let payload = &row["payload"];
    let ts = row["timestamp"].as_str();
    let turn = |suffix: &str| payload["turn_id"].as_str().map(|t| format!("{t}:{suffix}"));
    Ok(match payload["type"].as_str() {
        Some("task_started") => vec![lifecycle("turn_start", "", ts, turn("start").as_deref())],
        Some("task_complete") => vec![lifecycle("turn_end", "", ts, turn("end").as_deref())],
        Some("turn_aborted") => vec![lifecycle("turn_aborted", "", ts, turn("abort").as_deref())],
        Some("item_completed") => item(&payload["item"], ts).into_iter().collect(),
        _ => vec![],
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn local_sessions_codex_items_become_terminal_items() {
        let meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.149.0","source":"cli"}});
        assert!(normalize("s", &meta).unwrap().is_empty());
        assert!(normalize("other", &meta).is_err());
        let ts = "2026-09-12T10:00:00.000Z";
        let ev = |item: Value| json!({"type":"event_msg","timestamp":ts,"payload":{"type":"item_completed","thread_id":"s","turn_id":"t1","item":item}});
        let user = normalize("s", &ev(json!({"type":"UserMessage","id":"u1","content":[{"type":"text","text":"[Lightbulb teammate contribution from @aden (Aden Kim) {\"roomId\":\"r\",\"requestId\":\"q\",\"actorUserId\":\"user_a\"}]\nrun it"}]}))).unwrap();
        assert_eq!(
            (user[0]["text"].as_str(), user[0]["actor"].clone()),
            (Some("run it"), json!({"userId":"user_a"}))
        );
        let reasoning = normalize(
            "s",
            &ev(json!({"type":"Reasoning","id":"r1","summary_text":["**Planning the rerun**"]})),
        )
        .unwrap();
        assert_eq!(
            (reasoning[0]["kind"].as_str(), reasoning[0]["text"].as_str()),
            (Some("thinking"), Some("**Planning the rerun**"))
        );
        let output = (1..=25)
            .map(|i| format!("l{i}"))
            .collect::<Vec<_>>()
            .join("\n");
        let cmd = normalize("s", &ev(json!({"type":"CommandExecution","id":"e1","command":["/bin/zsh","-lc","git status"],"cwd":"file:///x","aggregated_output":output,"exit_code":1,"status":"completed"}))).unwrap();
        assert_eq!(cmd[0]["tool"]["name"], "CommandExecution");
        assert_eq!(cmd[0]["tool"]["input"]["command"], "git status");
        assert_eq!(cmd[0]["tool"]["status"], "error");
        assert_eq!(cmd[0]["tool"]["lines"], 25);
        assert_eq!(
            cmd[0]["text"].as_str().unwrap().lines().count(),
            PREVIEW_LINES
        );
        let change = normalize("s", &ev(json!({"type":"FileChange","id":"f1","changes":{"/Users/me/repo/src/a.ts":{"type":"update","diff":"-a\n+b"}}}))).unwrap();
        assert_eq!(change[0]["text"], "a.ts");
        assert_eq!(change[0]["tool"]["result"]["a.ts"]["diff"], "-a\n+b");
        let mcp = normalize("s", &ev(json!({"type":"McpToolCall","id":"m1","server":"codex_app","tool":"open","arguments":{"a":1},"status":"completed","result":{"content":[{"type":"text","text":"ok"}],"isError":false}}))).unwrap();
        assert_eq!(
            (mcp[0]["text"].as_str(), mcp[0]["tool"]["status"].as_str()),
            (Some("codex_app.open"), Some("done"))
        );
        let search = normalize(
            "s",
            &ev(json!({"type":"Extension","kind":"web.search","id":"w1","query":"k6 timeout"})),
        )
        .unwrap();
        assert_eq!(
            (
                search[0]["tool"]["name"].as_str(),
                search[0]["text"].as_str()
            ),
            (Some("web.search"), Some("k6 timeout"))
        );
        let secret_search = normalize(
            "s",
            &ev(json!({"type":"Extension","kind":"web.search","id":"w2","query":"find AKIAIOSFODNN7EXAMPLE"})),
        )
        .unwrap();
        let query = secret_search[0]["tool"]["input"]["query"].as_str().unwrap();
        assert!(query.contains("[redacted]"));
        assert!(!query.contains("AKIAIOSFODNN7EXAMPLE"));
        let final_answer = normalize("s", &ev(json!({"type":"AgentMessage","id":"a1","phase":"final_answer","content":[{"type":"Text","text":"Done."}]}))).unwrap();
        assert_eq!(
            (
                final_answer[0]["kind"].as_str(),
                final_answer[0]["text"].as_str()
            ),
            (Some("text"), Some("Done."))
        );
        // Absent phase is ordinary; only an unrecognized phase is withheld.
        let commentary = normalize("s", &ev(json!({"type":"AgentMessage","id":"a2","phase":"commentary","content":[{"type":"Text","text":"Thinking aloud."}]}))).unwrap();
        assert_eq!(commentary[0]["text"].as_str(), Some("Thinking aloud."));
        let bare = normalize("s", &ev(json!({"type":"AgentMessage","id":"a3","content":[{"type":"Text","text":"Still done."}]}))).unwrap();
        assert_eq!(bare[0]["text"].as_str(), Some("Still done."));
        assert!(normalize("s", &ev(json!({"type":"AgentMessage","id":"a4","phase":"analysis","content":[{"type":"Text","text":"scratch"}]}))).unwrap().is_empty());
        assert_eq!(
            normalize("s", &ev(json!({"type":"ContextCompaction","id":"c1"}))).unwrap()[0]
                ["lifecycle"],
            "compaction"
        );
        let started = json!({"type":"event_msg","timestamp":ts,"payload":{"type":"task_started","turn_id":"t1"}});
        assert_eq!(
            normalize("s", &started).unwrap()[0]["lifecycle"],
            "turn_start"
        );
        let aborted = json!({"type":"event_msg","timestamp":ts,"payload":{"type":"turn_aborted","turn_id":"t1","reason":"interrupted"}});
        assert_eq!(
            normalize("s", &aborted).unwrap()[0]["lifecycle"],
            "turn_aborted"
        );
        assert!(normalize("s", &json!({"type":"response_item","payload":{"type":"message","role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"legacy"}]}})).unwrap().is_empty());
        assert!(normalize(
            "s",
            &json!({"type":"event_msg","payload":{"type":"token_count"}})
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn local_sessions_codex_tail_state_reports_busy_idle_and_title() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout.jsonl");
        let started = json!({"type":"event_msg","payload":{"type":"task_started","turn_id":"t9"}});
        let complete =
            json!({"type":"event_msg","payload":{"type":"task_complete","turn_id":"t9"}});
        let user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"Check whether the timeout also happens at concurrency 50, please, and report back with numbers"}]}}});
        fs::write(&path, format!("{user}\n{started}\n")).unwrap();
        let (status, turn, title) = tail_state(&path);
        assert_eq!((status, turn.as_deref()), ("busy", Some("t9")));
        assert_eq!(title.as_deref().map(str::len), Some(60));
        fs::write(&path, format!("{user}\n{started}\n{complete}\n")).unwrap();
        assert_eq!(tail_state(&path).0, "idle");
        fs::write(&path, "{}\n").unwrap();
        assert_eq!(tail_state(&path), ("unknown", None, None));
        let secret_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"use sk-abcdefghijklmnopqrstuvwxyz12 please"}]}}});
        fs::write(&path, format!("{secret_user}\n")).unwrap();
        let title = tail_state(&path).2.unwrap();
        assert!(title.contains("[redacted]"));
        assert!(!title.contains("sk-abcdefghijklmnopqrstuvwxyz12"));
        let header_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"[Lightbulb teammate contribution from @aden (Aden Kim) {\"roomId\":\"r\"}]\nrun it"}]}}});
        let later_user = json!({"type":"event_msg","payload":{"type":"item_completed","item":{"type":"UserMessage","content":[{"type":"text","text":"plain later prompt"}]}}});
        fs::write(&path, format!("{header_user}\n{later_user}\n")).unwrap();
        assert_eq!(tail_state(&path).2.as_deref(), Some("plain later prompt"));
    }
    #[test]
    fn local_sessions_codex_discovers_only_process_owned_cli_profile_history() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("rollout-owned.jsonl");
        fs::write(&path, "{\"type\":\"session_meta\",\"payload\":{\"id\":\"owned\",\"source\":\"cli\",\"cli_version\":\"0.149.0\"}}\n{\"type\":\"event_msg\",\"payload\":{\"type\":\"task_started\",\"turn_id\":\"t1\"}}\n").unwrap();
        let process = Process {
            pid: 42,
            birth_micros: 123,
            executable: "/bin/codex".into(),
        };
        let found = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(found.conversation, "owned");
        assert_eq!(found.process.birth_micros, 123);
        assert_eq!(found.status, "busy");
        assert!(from_rollout(
            &root.join("other-profile"),
            process.clone(),
            &path,
            "0.149.0"
        )
        .is_err());
        // The backend caps cwd_name at 128 characters; a longer basename would
        // fail registration and loop the mirror.
        let long = "d".repeat(200);
        fs::write(&path, format!("{{\"type\":\"session_meta\",\"payload\":{{\"id\":\"owned\",\"source\":\"cli\",\"cli_version\":\"0.149.0\",\"cwd\":\"/tmp/{long}\"}}}}\n")).unwrap();
        let bounded_cwd = from_rollout(&root, process.clone(), &path, "0.149.0").unwrap();
        assert_eq!(bounded_cwd.cwd_name.unwrap().chars().count(), 128);
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
