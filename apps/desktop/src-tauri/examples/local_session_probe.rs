//! Disposable-session feasibility probe. See the native-probe results document for reruns.
use lightbulb_desktop::local_sessions::{supported_claude_version, verify_claude_runtime, MAX_JSONL_ROW};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{MetadataExt, OpenOptionsExt},
        net::UnixStream,
    },
    path::Path,
    process::Command,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, String>;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
struct Identity {
    provider: String,
    installed_version: String,
    session: String,
    pid: u32,
    generation: String,
    cwd: String,
    transcript: String,
    socket: Option<String>,
    socket_inode: Option<u64>,
}

fn validate(expected: &Identity, actual: &Identity) -> Result<()> {
    match (actual.provider.as_str(), actual.installed_version.as_str()) {
        ("claude", version) if supported_claude_version(version) => (),
        ("codex", "0.149.0") => (),
        _ => {
            return Err("unsupported installed provider version; rerun feasibility research".into())
        }
    }
    if expected != actual {
        return Err("session/process/generation identity changed".into());
    }
    Ok(())
}

#[derive(Default)]
struct JsonLines(Vec<u8>);
impl JsonLines {
    fn push(&mut self, bytes: &[u8]) -> Result<Vec<Value>> {
        self.0.extend_from_slice(bytes);
        let mut rows = vec![];
        while let Some(end) = self.0.iter().position(|b| *b == b'\n') {
            if end + 1 > MAX_JSONL_ROW {
                return Err("JSON line exceeds probe limit".into());
            }
            rows.push(serde_json::from_slice(&self.0[..end]).map_err(|e| e.to_string())?);
            self.0.drain(..=end);
        }
        if self.0.len() > MAX_JSONL_ROW {
            return Err("partial JSON line exceeds probe limit".into());
        }
        Ok(rows)
    }
}

fn message(provider: &str, session: &str, row: &Value) -> Result<Vec<Value>> {
    Ok(lightbulb_desktop::local_sessions::normalize_public_message(provider, session, row)?
        .into_iter()
        .map(|event| json!({"role":event["role"],"kind":event["kind"],"text":event["text"],"timestamp":row["timestamp"]}))
        .collect())
}

fn command(program: &str, args: &[&str]) -> Result<String> {
    let out = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!(
            "{program} failed: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    String::from_utf8(out.stdout).map_err(|e| e.to_string())
}

fn generation(pid: u32) -> Result<String> {
    let value = command("ps", &["-p", &pid.to_string(), "-o", "lstart=,comm="])?;
    if value.trim().is_empty() {
        return Err("process exited".into());
    }
    Ok(value.trim().to_string())
}

fn claude_rows() -> Result<Vec<Value>> {
    serde_json::from_str(&command("claude", &["agents", "--json"])?).map_err(|e| e.to_string())
}

fn capture(provider: &str, pid: u32, transcript: Option<&str>) -> Result<Identity> {
    if !matches!(provider, "claude" | "codex") {
        return Err("unsupported provider".into());
    }
    let before = generation(pid)?;
    let version_output = command(provider, &["--version"])?;
    let version = if provider == "claude" {
        version_output.split_whitespace().next()
    } else {
        version_output.split_whitespace().nth(1)
    }
    .ok_or("missing version")?
    .to_string();
    let (session, cwd, transcript, socket) = if provider == "claude" {
        let row = claude_rows()?
            .into_iter()
            .find(|r| r["pid"].as_u64() == Some(pid.into()))
            .ok_or("native Claude listing has no such process")?;
        let transcript = transcript
            .ok_or("Claude requires transcript_path from its temporary SessionStart hook")?
            .to_string();
        let sockets = command("lsof", &["-n", "-a", "-p", &pid.to_string(), "-U", "-Fn"])?;
        let socket = sockets
            .lines()
            .filter_map(|l| l.strip_prefix('n'))
            .find(|p| p.ends_with(&format!("/{pid}.sock")))
            .ok_or("native inbox socket missing")?
            .to_string();
        (
            row["sessionId"]
                .as_str()
                .ok_or("missing session")?
                .to_string(),
            row["cwd"].as_str().ok_or("missing cwd")?.to_string(),
            transcript,
            Some(socket),
        )
    } else if provider == "codex" {
        let files = command("lsof", &["-n", "-a", "-p", &pid.to_string(), "-Fn"])?;
        let paths: Vec<_> = files
            .lines()
            .filter_map(|l| l.strip_prefix('n'))
            .filter(|p| p.ends_with(".jsonl") && p.contains("/rollout-"))
            .collect();
        if paths.len() != 1 {
            return Err("expected one open native rollout; submit a first terminal prompt".into());
        }
        let first = BufReader::new(File::open(paths[0]).map_err(|e| e.to_string())?)
            .lines()
            .next()
            .ok_or("empty rollout")?
            .map_err(|e| e.to_string())?;
        let row: Value = serde_json::from_str(&first).map_err(|e| e.to_string())?;
        if row["type"] != "session_meta"
            || row["payload"]["source"] != "cli"
            || row["payload"]["cli_version"] != version
        {
            return Err("not an ordinary matching-version native CLI rollout".into());
        }
        (
            row["payload"]["id"]
                .as_str()
                .ok_or("missing session")?
                .to_string(),
            row["payload"]["cwd"]
                .as_str()
                .ok_or("missing cwd")?
                .to_string(),
            paths[0].to_string(),
            None,
        )
    } else {
        return Err("provider must be claude or codex".into());
    };
    let socket_inode = socket
        .as_ref()
        .map(|p| fs::metadata(p).map(|m| m.ino()).map_err(|e| e.to_string()))
        .transpose()?;
    let identity = Identity {
        provider: provider.into(),
        installed_version: version,
        session,
        pid,
        generation: before,
        cwd,
        transcript,
        socket,
        socket_inode,
    };
    if generation(pid)? != identity.generation {
        return Err("process changed during capture".into());
    }
    validate(&identity, &identity)?;
    Ok(identity)
}

fn load(path: &str) -> Result<Identity> {
    let identity: Identity = serde_json::from_slice(&fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    validate(&identity, &identity)?;
    validate(
        &identity,
        &capture(&identity.provider, identity.pid, Some(&identity.transcript))?,
    )?;
    if identity.provider == "claude" {
        let since = claude_generation_cutoff(&identity)?;
        let runtime =
            verify_claude_runtime(Path::new(&identity.transcript), &identity.session, &since)?;
        if generation(identity.pid)? != identity.generation {
            return Err("process changed during version verification".into());
        }
        eprintln!(
            "runtime_version={runtime} installed_version={}",
            identity.installed_version
        );
    }
    Ok(identity)
}

fn claude_generation_cutoff(identity: &Identity) -> Result<String> {
    let row = claude_rows()?
        .into_iter()
        .find(|r| r["pid"] == identity.pid && r["sessionId"] == identity.session)
        .ok_or("native Claude execution missing")?;
    let registered_ms = row["startedAt"]
        .as_u64()
        .ok_or("missing native execution start")?;
    process_generation_cutoff(identity.pid, registered_ms)
}

fn process_generation_cutoff(pid: u32, registered_ms: u64) -> Result<String> {
    // OS start also bounds resumed history if native startedAt ever refers to the conversation.
    let start = command(
        "env",
        &["TZ=UTC", "ps", "-p", &pid.to_string(), "-o", "lstart="],
    )?;
    let seconds: u64 = command(
        "date",
        &["-u", "-j", "-f", "%a %b %e %T %Y", start.trim(), "+%s"],
    )?
    .trim()
    .parse()
    .map_err(|_| "cannot verify OS process start")?;
    // ps has second precision: exclude its entire ambiguous birth second, failing closed.
    let cutoff = registered_ms.max((seconds + 1) * 1000);
    let prefix = command(
        "date",
        &[
            "-u",
            "-r",
            &(cutoff / 1000).to_string(),
            "+%Y-%m-%dT%H:%M:%S",
        ],
    )?;
    Ok(format!("{}.{:03}Z", prefix.trim(), cutoff % 1000))
}

fn save(path: &str, identity: &Identity) -> Result<()> {
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(|e| e.to_string())?;
    f.write_all(&serde_json::to_vec_pretty(identity).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

fn run() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let start = Instant::now();
    match args.get(1).map(String::as_str) {
        Some("discover") if args.len() == 4 => {
            let cwd = fs::canonicalize(&args[3]).map_err(|e| e.to_string())?;
            if args[2] == "claude" {
                for row in claude_rows()? { if row["cwd"].as_str().is_some_and(|p| Path::new(p) == cwd) { println!("{row}"); } }
            } else if args[2] == "codex" {
                for line in command("ps", &["-axo", "pid=,comm="])?.lines() {
                    let mut parts = line.split_whitespace();
                    let pid = parts.next().unwrap_or("");
                    if !parts.next().unwrap_or("").ends_with("/codex") { continue; }
                    let Ok(open_cwd) = command("lsof", &["-a", "-p", pid, "-d", "cwd", "-Fn"]) else { continue; };
                    if !open_cwd.lines().any(|l| l.strip_prefix('n').is_some_and(|p| Path::new(p) == cwd)) { continue; }
                    match capture("codex", pid.parse().map_err(|_| "invalid process id")?, None) {
                        Ok(id) => println!("{}", serde_json::to_string(&id).unwrap()),
                        Err(e) => eprintln!("pid {pid}: {e}"),
                    }
                }
            } else { return Err("provider must be claude or codex".into()); }
            eprintln!("discovery_ms={}", start.elapsed().as_millis());
        }
        Some("capture") if args.len() == 5 || args.len() == 6 => {
            let identity = capture(&args[2], args[3].parse().map_err(|_| "invalid pid")?, args.get(5).map(String::as_str))?;
            save(&args[4], &identity)?;
        }
        Some("hook") if args.len() == 3 => {
            let hook: Value = serde_json::from_reader(std::io::stdin().take(1024 * 1024)).map_err(|e| e.to_string())?;
            let socket = std::env::var("CLAUDE_CODE_MESSAGING_SOCKET").map_err(|_| "native socket unavailable")?;
            let pid: u32 = Path::new(&socket).file_stem().and_then(|s| s.to_str()).ok_or("socket has no pid")?.parse().map_err(|_| "invalid socket pid")?;
            let identity = capture("claude", pid, hook["transcript_path"].as_str())?;
            if hook["session_id"] != identity.session || hook["cwd"] != identity.cwd || identity.socket.as_deref() != Some(socket.as_str()) { return Err("hook/native identity mismatch".into()); }
            save(&args[2], &identity)?;
        }
        Some("send") if args.len() == 4 => {
            let identity = load(&args[2])?;
            if args[3].trim().is_empty() || args[3].len() > 32 * 1024 { return Err("message must contain 1..32768 bytes".into()); }
            if identity.provider == "codex" {
                let out = command("codex", &["queue", "--thread", &identity.session, "--message", &args[3]])?;
                println!("{}", json!({"admission":"queued_not_delivery_ack","native_result":out.trim(),"elapsed_ms":start.elapsed().as_millis()}));
            } else {
                let mut socket = UnixStream::connect(identity.socket.as_ref().ok_or("missing socket")?).map_err(|e| e.to_string())?;
                socket.set_write_timeout(Some(Duration::from_secs(3))).map_err(|e| e.to_string())?;
                // Verified 2.1.268/269 peer envelope, not a stable published API. No auth/own-child token.
                let wire = json!({"type":"user","from":"lightbulb-native-probe","message":{"role":"user","content":args[3]}});
                writeln!(socket, "{wire}").map_err(|e| e.to_string())?;
                println!("{}", json!({"admission":"socket_written_delivery_unknown","elapsed_ms":start.elapsed().as_millis()}));
            }
        }
        Some("watch") if args.len() == 4 => {
            let identity = load(&args[2])?;
            let duration: u64 = args[3].parse().map_err(|_| "invalid duration seconds")?;
            if duration > 300 { return Err("probe watch is limited to 300 seconds".into()); }
            let mut file = File::open(&identity.transcript).map_err(|e| e.to_string())?;
            let inode = file.metadata().map_err(|e| e.to_string())?.ino();
            let mut decoder = JsonLines::default();
            let mut offset = 0u64;
            loop {
                if generation(identity.pid)? != identity.generation { return Err("process generation changed".into()); }
                let meta = fs::metadata(&identity.transcript).map_err(|e| e.to_string())?;
                if meta.ino() != inode || meta.len() < offset { return Err("transcript replaced or truncated".into()); }
                let mut bytes = [0u8; 65536];
                let n = file.read(&mut bytes).map_err(|e| e.to_string())?;
                offset += n as u64;
                for row in decoder.push(&bytes[..n])? {
                    for mut event in message(&identity.provider, &identity.session, &row)? {
                        event["observed_elapsed_ms"] = json!(start.elapsed().as_millis());
                        println!("{event}");
                    }
                }
                if start.elapsed() >= Duration::from_secs(duration) { break; }
                // ponytail: 100 ms file polling; native streaming API only if token deltas become required.
                if n == 0 { std::thread::park_timeout(Duration::from_millis(100)); }
            }
        }
        _ => return Err("usage: discover <claude|codex> <cwd> | capture <provider> <pid> <new-registration.json> [claude-transcript] | hook <new-registration.json> | send <registration.json> <text> | watch <registration.json> <seconds>".into()),
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn verify_running_claude(
        identity: &Identity,
        since: &str,
        mut reader: impl Read,
    ) -> Result<String> {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.jsonl");
        std::io::copy(&mut reader, &mut File::create(&path).unwrap()).unwrap();
        verify_claude_runtime(&path, &identity.session, since)
    }
    fn identity() -> Identity {
        Identity {
            provider: "claude".into(),
            installed_version: "2.1.268".into(),
            session: "synthetic-session".into(),
            pid: 42,
            generation: "start-1".into(),
            cwd: "/tmp/probe".into(),
            transcript: "/tmp/probe/log.jsonl".into(),
            socket: Some("/tmp/probe/inbox.sock".into()),
            socket_inode: Some(123),
        }
    }

    #[test]
    fn partial_utf8_and_multiple_frames_are_not_lost_or_duplicated() {
        let bytes = "{\"text\":\"café\"}\n{\"n\":2}\n{\"n\":".as_bytes();
        for split in 0..=bytes.len() {
            let mut decoder = JsonLines::default();
            let mut rows = decoder.push(&bytes[..split]).unwrap();
            rows.extend(decoder.push(&bytes[split..]).unwrap());
            assert_eq!(rows, vec![json!({"text":"café"}), json!({"n":2})]);
            assert_eq!(decoder.push(b"3}\n").unwrap(), vec![json!({"n":3})]);
            assert!(decoder.push(b"").unwrap().is_empty());
        }
        assert!(JsonLines::default().push(b"garbage\n").is_err());
        let large = format!("{}\n", json!({"padding":"x".repeat(2_200_000)}));
        assert_eq!(JsonLines::default().push(large.as_bytes()).unwrap().len(), 1);
        assert!(JsonLines::default()
            .push(&vec![b'x'; MAX_JSONL_ROW + 1])
            .is_err());
    }

    #[test]
    fn stale_identity_and_unsupported_versions_are_rejected() {
        let good = identity();
        assert!(validate(&good, &good).is_ok());
        for key in [
            "session",
            "pid",
            "generation",
            "socket_inode",
            "cwd",
            "transcript",
            "installed_version",
            "provider",
        ] {
            let mut wrong = serde_json::to_value(&good).unwrap();
            wrong[key] = if key == "pid" || key == "socket_inode" {
                json!(99)
            } else {
                json!("different")
            };
            assert!(
                validate(&good, &serde_json::from_value(wrong).unwrap()).is_err(),
                "{key}"
            );
        }
        let mut unsupported = good;
        unsupported.installed_version = "999.0.0".into();
        assert!(validate(&unsupported, &unsupported).is_err());
    }

    #[test]
    fn unsupported_registration_is_rejected_before_running_any_provider_command() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("registration.json");
        let mut unsupported = identity();
        unsupported.provider = "/must-not-execute".into();
        save(path.to_str().unwrap(), &unsupported).unwrap();
        assert!(load(path.to_str().unwrap())
            .unwrap_err()
            .contains("unsupported"));
    }

    #[test]
    fn observed_claude_messages_validate_session_and_ignore_nontext() {
        let row = json!({"type":"assistant","sessionId":"s","version":"2.1.268","uuid":"m",
            "message":{"role":"assistant","content":[{"type":"text","text":"hello"},{"type":"thinking","signature":"never-export"}]}});
        assert_eq!(message("claude", "s", &row).unwrap()[0]["text"], "hello");
        assert!(message("claude", "other", &row).is_err());
        let mut missing = row.clone();
        missing.as_object_mut().unwrap().remove("sessionId");
        assert!(message("claude", "s", &missing).is_err());
        let mut missing = row.clone();
        missing.as_object_mut().unwrap().remove("version");
        assert!(message("claude", "s", &missing).is_err());
    }

    #[test]
    fn observed_codex_metadata_and_completed_message_are_distinct() {
        let meta = json!({"type":"session_meta","payload":{"id":"s","cli_version":"0.149.0"}});
        assert!(message("codex", "s", &meta).unwrap().is_empty());
        assert!(message("codex", "other", &meta).is_err());
        let row = json!({"type":"event_msg","timestamp":"2026-09-11T00:00:00Z","payload":{
            "type":"item_completed","thread_id":"s","turn_id":"t1","item":{"type":"AgentMessage","id":"m","phase":"final_answer","content":[{"type":"Text","text":"done"}]}}});
        assert_eq!(message("codex", "s", &row).unwrap()[0]["text"], "done");
        // Replay observed envelope shapes through fragmented input; UTF-8 splits are covered above.
        let wire = format!("{meta}\n{row}\n");
        for split in 0..=wire.len() {
            let mut decoder = JsonLines::default();
            let mut rows = decoder.push(&wire.as_bytes()[..split]).unwrap();
            rows.extend(decoder.push(&wire.as_bytes()[split..]).unwrap());
            let events: Vec<_> = rows
                .iter()
                .flat_map(|r| message("codex", "s", r).unwrap())
                .collect();
            assert_eq!(events.len(), 1);
            assert_eq!(events[0]["text"], "done");
        }
    }

    #[test]
    fn claude_269_install_does_not_relabel_the_running_version() {
        let mut id = identity();
        id.installed_version = "2.1.269".into();
        assert!(validate(&id, &id).is_ok());
        let since = "2026-09-11T19:20:16.000Z";
        for runtime in ["2.1.268", "2.1.269"] {
            let row = json!({"type":"user","sessionId":"synthetic-session","timestamp":since,"version":runtime});
            assert_eq!(
                verify_running_claude(&id, since, format!("{row}\n").as_bytes()).map(|v| json!(v)),
                Ok(json!(runtime))
            );
        }
    }

    #[test]
    fn installed_claude_version_cannot_approve_an_older_running_generation() {
        let id = identity();
        assert!(validate(&id, &id).is_ok()); // Supported PATH binary, not proof of runtime version.
        let since = "2026-09-11T03:00:00.500Z";
        let history = "{\"type\":\"assistant\",\"sessionId\":\"synthetic-session\",\"timestamp\":\"2026-09-10T03:00:00.000Z\",\"version\":\"2.1.268\"}\n";
        let running = "{\"type\":\"user\",\"sessionId\":\"synthetic-session\",\"timestamp\":\"2026-09-11T03:00:01.000Z\",\"version\":\"2.1.100\"}\n";
        assert!(
            verify_running_claude(&id, since, format!("{history}{running}").as_bytes()).is_err()
        );
        assert!(verify_running_claude(&id, since, history.as_bytes()).is_err());
        assert!(verify_running_claude(&id, since, b"".as_slice()).is_err());
        let supported = running.replace("2.1.100", "2.1.268");
        assert!(verify_running_claude(&id, since, supported.as_bytes()).is_ok());
        let wrong_session = supported.replace("synthetic-session", "other");
        assert!(verify_running_claude(&id, since, wrong_session.as_bytes()).is_err());
        let unknown_timestamp = supported.replace("03:00:01.000Z", "03:00:01.BADZ");
        assert!(verify_running_claude(&id, since, unknown_timestamp.as_bytes()).is_err());
    }

    #[test]
    fn os_birth_cutoff_uses_the_actual_test_process() {
        let cutoff = process_generation_cutoff(std::process::id(), 0).unwrap();
        assert_eq!(cutoff.len(), 24);
        assert!(cutoff.ends_with(".000Z")); // OS birth's ambiguous second is excluded.
        let seconds: u64 = command(
            "date",
            &["-u", "-j", "-f", "%Y-%m-%dT%H:%M:%S", &cutoff[..19], "+%s"],
        )
        .unwrap()
        .trim()
        .parse()
        .unwrap();
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        assert!(seconds <= now + 1 && seconds + 60 >= now);
        assert_eq!(
            process_generation_cutoff(std::process::id(), 4102444800123).unwrap(),
            "2100-01-01T00:00:00.123Z"
        );
    }

    #[test]
    fn unknown_or_nonpublic_message_shapes_never_emit_text() {
        let claude = json!({"type":"assistant","sessionId":"s","version":"2.1.268",
            "message":{"role":"assistant","content":[{"type":"text","text":"public"}]}});
        let codex = json!({"type":"event_msg","payload":{"type":"item_completed","thread_id":"s",
            "turn_id":"t1","item":{"type":"AgentMessage","id":"a1","phase":"final_answer","content":[{"type":"Text","text":"public"}]}}});
        for phase in ["commentary", "final_answer"] {
            let mut row = codex.clone();
            row["payload"]["item"]["phase"] = json!(phase);
            assert_eq!(message("codex", "s", &row).unwrap()[0]["text"], "public");
        }
        for (provider, base, path, value) in [
            ("claude", &claude, "/type", json!("unknown")),
            ("claude", &claude, "/message/role", json!("user")),
            ("claude", &claude, "/message/channel", json!("analysis")),
            ("claude", &claude, "/message/phase", json!("unknown")),
            ("claude", &claude, "/channel", json!("analysis")),
            ("codex", &codex, "/type", json!("unknown")),
            ("codex", &codex, "/payload/type", json!("unknown")),
            ("codex", &codex, "/payload/item/type", json!("unknown")),
            ("codex", &codex, "/payload/item/content", json!([])),
            ("codex", &codex, "/phase", json!("unknown")),
        ] {
            let mut row = base.clone();
            let (parent, key) = path.rsplit_once('/').unwrap();
            row.pointer_mut(parent)
                .unwrap()
                .as_object_mut()
                .unwrap()
                .insert(key.into(), value);
            assert!(
                message(provider, "s", &row).unwrap().is_empty(),
                "{provider} {path}"
            );
        }
    }
}
