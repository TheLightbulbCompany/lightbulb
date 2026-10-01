//! Remote answers to a Claude Code permission prompt (owner ruling 2026-09-25).
//!
//! The `PermissionRequest` hook is the one channel Claude Code reads a decision
//! from, and it runs WHILE the terminal shows the prompt (verified live on
//! 2.1.282: the dialog is on screen during the hook, a hook's late answer still
//! resolves it, and answering in the terminal kills the hook). So the hook can
//! hold the prompt open for a remote answer without taking the terminal's away.
//!
//! What keeps that safe:
//! - The hook answers only with a decision the desktop app delivered for THIS
//!   prompt: its random approval id and the SHA-256 of the exact tool input.
//! - The decision is built here, from a closed set: allow, allow + a
//!   session-scoped mode Claude itself suggested, or deny. Never `updatedInput`,
//!   never a persistent rule -- a remote approver cannot widen what runs.
//! - "Exact native approval": a shell command can be approved remotely only
//!   when the approver saw all of it -- one line, uncut and unredacted.
//!   Anything else is deny-only from Lightbulb and approved in the terminal.
//! - The app answers only while it holds a live, shared mirror of the session,
//!   and only on a command the backend admitted for an authorized human.
use super::*;
use std::os::unix::net::UnixDatagram;
use std::time::{Duration, Instant};

/// How long a prompt stays answerable from Lightbulb. The installed hook's own
/// timeout is longer, so the hook always releases the prompt itself.
pub(crate) const HOLD: Duration = Duration::from_secs(55 * 60);
/// Installed as the hook's `timeout` (seconds).
pub(crate) const HOOK_TIMEOUT_SECS: u64 = 60 * 60;
/// The app acknowledges a prompt it can relay within this, or the hook lets go.
const ACK_WAIT: Duration = Duration::from_secs(2);
const MODES: [&str; 3] = ["acceptEdits", "plan", "default"];
/// Read-only tools whose one-line summary IS the thing being approved. An edit
/// is not among them: its summary is the path, not the content being written,
/// so it is approved in the terminal and deny-only from Lightbulb. Grep and Glob
/// show a pattern but not the tree they search, so they are deny-only too, as
/// is every tool whose summary is a sample of its input.
const TARGET_TOOLS: [&str; 3] = ["Read", "WebFetch", "WebSearch"];

/// A prompt the hook is holding, as the app relays it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct Held {
    pub id: String,
    pub input_digest: String,
    pub reply: String,
    pub exact: bool,
    #[serde(default)]
    pub modes: Vec<String>,
}

/// The answer the app delivers to a holding hook.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct Answer {
    pub approval_id: String,
    pub input_digest: String,
    pub decision: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

/// Only a real permission prompt is held. A question (`AskUserQuestion`) is
/// answered with words, not allow/deny, and stays in the terminal.
pub(crate) fn holdable(provider: &str, stdin: &Value) -> bool {
    provider == "claude"
        && stdin["hook_event_name"] == "PermissionRequest"
        && stdin["tool_name"]
            .as_str()
            .is_some_and(|t| !t.is_empty() && t != "AskUserQuestion")
        && stdin["session_id"].as_str().is_some_and(|s| !s.is_empty())
}

pub(crate) fn digest(input: &Value) -> String {
    let bytes = serde_json::to_vec(&lifecycle::canonical(input)).unwrap_or_default();
    hex::encode(Sha256::digest(bytes))
}

/// Full arguments belong only to the held permission prompt. Ordinary tool
/// transcript rows keep Claude's curated, bounded summary.
pub(crate) fn display_summary(tool: &str, input: &Value, cwd: Option<&str>, home: Option<&str>) -> String {
    if let Some(name @ ("list_targets" | "receipt")) = tool.strip_prefix("mcp__lightbulb_native_sessions__") {
        if super::native_mcp::allowed(name, input) {
            return serde_json::to_string(&lifecycle::canonical(input))
                .map(|args| format!("{name} {args}"))
                .unwrap_or_default();
        }
    }
    claude::summary(tool, input, cwd, home)
}

/// Can an approver who sees the summary see everything they are approving?
/// The summary must be exactly what is relayed: whole, unredacted, and plain
/// printable ASCII for a command (no control, bidi or zero-width characters
/// that render one thing and run another).
pub(crate) fn exact(tool: &str, input: &Value, cwd: Option<&str>, home: Option<&str>) -> bool {
    let shown = |text: &str| {
        !text.trim().is_empty()
            && text.len() <= lifecycle::WAITING_TEXT_MAX
            && redact(text) == text
            && text.bytes().all(|b| (0x20..0x7f).contains(&b))
    };
    // ponytail: shared approval rows cannot carry a private `send` body; that
    // tool stays terminal-only until an owner-private approval wire exists.
    if let Some(name) = tool.strip_prefix("mcp__lightbulb_native_sessions__") {
        return matches!(name, "list_targets" | "receipt")
            && super::native_mcp::allowed(name, input)
            && input.as_object().is_some_and(|args| {
                args.values().all(|value| {
                    value.as_str().is_some_and(|text| {
                        text.bytes().all(|b| (0x20..0x7f).contains(&b)) && redact(text) == text
                    })
                })
            })
            && shown(&display_summary(tool, input, cwd, home));
    }
    match tool {
        "Bash" => input["command"].as_str().is_some_and(shown),
        // A path is shown relative to the checkout or home; anywhere else only its
        // basename is, which is not the file being read.
        "Read" => input["file_path"].as_str().is_some_and(|path| {
            let within = |root: Option<&str>| {
                root.is_some_and(|r| {
                    path.strip_prefix(r)
                        .is_some_and(|rest| rest.starts_with('/'))
                })
            };
            (within(cwd) || within(home)) && shown(&claude::summary(tool, input, cwd, home))
        }),
        tool if TARGET_TOOLS.contains(&tool) => shown(&claude::summary(tool, input, cwd, home)),
        _ => false,
    }
}

/// The session-scoped mode switches Claude itself offered for this prompt. A
/// shell prompt offers none: "approve this command" must not widen into "and
/// everything after it".
pub(crate) fn modes(tool: &str, suggestions: &Value) -> Vec<String> {
    if tool == "Bash" || tool.starts_with("mcp__lightbulb_native_sessions__") {
        return vec![];
    }
    let mut out: Vec<String> = suggestions
        .as_array()
        .into_iter()
        .flatten()
        .filter(|s| s["type"] == "setMode" && s["destination"] == "session")
        .filter_map(|s| s["mode"].as_str())
        .filter(|m| MODES.contains(m))
        .map(str::to_owned)
        .collect();
    out.dedup();
    out
}

/// A reply channel the app may write to: inside the private hook directory,
/// named for one approval. Anything else is refused, whatever a datagram says.
pub(crate) fn reply_allowed(dir: &Path, reply: &str, id: &str) -> bool {
    let path = Path::new(reply);
    valid_id(id)
        && path.parent() == Some(dir)
        && path.file_name().and_then(|n| n.to_str()) == Some(&format!("{id}.sock"))
}

pub(crate) fn valid_id(id: &str) -> bool {
    id.strip_prefix("appr-").is_some_and(|hex| {
        hex.len() == 32
            && hex
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    })
}

/// The hook's stdout document for one delivered answer.
pub(crate) fn decision(answer: &Answer) -> Option<Value> {
    let decision = match answer.decision.as_str() {
        "allow" => {
            let mut d = json!({"behavior":"allow"});
            if let Some(mode) = answer.mode.as_deref() {
                if !MODES.contains(&mode) {
                    return None;
                }
                d["updatedPermissions"] =
                    json!([{"type":"setMode","mode":mode,"destination":"session"}]);
            }
            d
        }
        "deny" if answer.mode.is_none() => {
            let message: String = answer
                .message
                .as_deref()
                .unwrap_or("Denied from Lightbulb.")
                .chars()
                .take(600)
                .collect();
            json!({"behavior":"deny","message":message})
        }
        _ => return None,
    };
    Some(json!({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":decision}}))
}

/// Deliver an answer (or a bare probe, `None`) to a holding hook. `false` means
/// nobody is holding that prompt any more: it was answered in the terminal, or
/// the hook let it go.
pub(crate) fn deliver(reply: &str, answer: Option<&Answer>) -> bool {
    let body = answer
        .and_then(|a| serde_json::to_vec(a).ok())
        .unwrap_or_default();
    UnixDatagram::unbound().is_ok_and(|socket| {
        let _ = socket.set_write_timeout(Some(Duration::from_millis(100)));
        socket.send_to(&body, reply).is_ok()
    })
}

/// Tell a holding hook the app is relaying its prompt.
pub(crate) fn acknowledge(reply: &str, id: &str) -> bool {
    UnixDatagram::unbound().is_ok_and(|socket| {
        let _ = socket.set_write_timeout(Some(Duration::from_millis(100)));
        socket
            .send_to(json!({"hold":id}).to_string().as_bytes(), reply)
            .is_ok()
    })
}

/// The hook's half: report the prompt, wait for the app to take it, then wait
/// for an answer that names this exact prompt. Writes at most one decision to
/// `answer_fd` and exits silently on every other path, leaving the prompt to
/// the terminal.
pub(crate) fn hold(
    wake: &Path,
    provider: &str,
    stdin: &Value,
    answer_fd: std::os::unix::io::RawFd,
) {
    use std::io::Write;
    use std::os::unix::io::FromRawFd;
    // Owned from the first line, so every exit path closes it: an answer or nothing.
    let mut out = unsafe { std::fs::File::from_raw_fd(answer_fd) };
    let Some(dir) = wake.parent() else { return };
    if private_dir(dir).is_err() {
        return;
    }
    let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
    let path = dir.join(format!("{id}.sock"));
    let Ok(socket) = UnixDatagram::bind(&path) else {
        return;
    };
    let _guard = Remove(path.clone());
    let tool = stdin["tool_name"].as_str().unwrap_or("");
    let held = Held {
        id: id.clone(),
        input_digest: digest(&stdin["tool_input"]),
        reply: path.to_string_lossy().into_owned(),
        exact: exact(
            tool,
            &stdin["tool_input"],
            stdin["cwd"].as_str(),
            std::env::var("HOME").ok().as_deref(),
        ),
        modes: modes(tool, &stdin["permission_suggestions"]),
    };
    let datagram = lifecycle::build_held_datagram(provider, stdin, &held);
    if !UnixDatagram::unbound().is_ok_and(|s| {
        let _ = s.set_write_timeout(Some(Duration::from_millis(100)));
        s.send_to(&datagram, wake).is_ok()
    }) {
        return;
    }
    let mut buf = [0u8; 4096];
    let started = Instant::now();
    let mut taken = false;
    loop {
        let limit = if taken { HOLD } else { ACK_WAIT };
        let Some(left) = limit.checked_sub(started.elapsed()) else {
            break;
        };
        if socket
            .set_read_timeout(Some(left.max(Duration::from_millis(1))))
            .is_err()
        {
            return;
        }
        let Ok(n) = socket.recv(&mut buf) else { break };
        let Ok(body) = serde_json::from_slice::<Value>(&buf[..n]) else {
            continue; // a probe
        };
        if !taken {
            taken = body["hold"] == id.as_str();
            continue;
        }
        let Ok(answer) = serde_json::from_value::<Answer>(body) else {
            continue;
        };
        if answer.approval_id != held.id || answer.input_digest != held.input_digest {
            continue;
        }
        if answer.decision == "allow" && !held.exact {
            continue;
        }
        if answer
            .mode
            .as_ref()
            .is_some_and(|m| !held.modes.contains(m))
        {
            continue;
        }
        let Some(document) = decision(&answer) else {
            continue;
        };
        // The one write this process ever makes to Claude's control channel.
        let _ = out.write_all(document.to_string().as_bytes());
        let _ = out.flush();
        return;
    }
    if taken {
        // Let the app stop offering an answer nobody is waiting for.
        let release = lifecycle::build_release_datagram(provider, stdin, &held.id);
        if let Ok(s) = UnixDatagram::unbound() {
            let _ = s.set_write_timeout(Some(Duration::from_millis(100)));
            let _ = s.send_to(&release, wake);
        }
    }
}

struct Remove(PathBuf);
impl Drop for Remove {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "appr-0123456789abcdef0123456789abcdef";

    fn answer(decision: &str) -> Answer {
        Answer {
            approval_id: ID.into(),
            input_digest: "d".repeat(64),
            decision: decision.into(),
            mode: None,
            message: None,
        }
    }

    #[test]
    fn local_sessions_approval_holds_only_real_claude_permission_prompts() {
        let prompt =
            json!({"hook_event_name":"PermissionRequest","tool_name":"Bash","session_id":"s"});
        assert!(holdable("claude", &prompt));
        assert!(!holdable("codex", &prompt));
        assert!(!holdable(
            "claude",
            &json!({"hook_event_name":"PermissionRequest","tool_name":"AskUserQuestion","session_id":"s"})
        ));
        assert!(!holdable(
            "claude",
            &json!({"hook_event_name":"PreToolUse","tool_name":"Bash","session_id":"s"})
        ));
        assert!(!holdable(
            "claude",
            &json!({"hook_event_name":"PermissionRequest","tool_name":"Bash"})
        ));
    }

    fn exact(tool: &str, input: &Value) -> bool {
        super::exact(tool, input, Some("/w"), Some("/Users/me"))
    }

    #[test]
    fn local_sessions_approval_shell_is_exact_only_when_seen_whole() {
        assert!(exact("Bash", &json!({"command":"git push origin main"})));
        assert!(
            !exact("Bash", &json!({"command":"echo a\nrm -rf /"})),
            "a second line is hidden"
        );
        assert!(
            !exact(
                "Bash",
                &json!({"command":"x".repeat(lifecycle::WAITING_TEXT_MAX + 1)})
            ),
            "cut"
        );
        assert!(
            !exact(
                "Bash",
                &json!({"command":"curl -H 'Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789'"})
            ),
            "a redacted command is not the command"
        );
        assert!(
            !exact("Bash", &json!({"command":"echo ok \u{202e}fr- mr"})),
            "bidi override"
        );
        assert!(
            !exact("Bash", &json!({"command":"echo ok\rrm -rf ~"})),
            "carriage return"
        );
        assert!(
            !exact("Bash", &json!({"command":"echo caf\u{e9}"})),
            "non-ASCII is deny-only"
        );
        assert!(exact("Read", &json!({"file_path":"/w/src/main.rs"})));
        assert!(
            !exact(
                "Edit",
                &json!({"file_path":"/w/src/main.rs","new_string":"x"})
            ),
            "the content is unseen"
        );
        assert!(!exact(
            "Write",
            &json!({"file_path":"/w/.zshrc","content":"x"})
        ));
        assert!(
            !exact(
                "Read",
                &json!({"file_path":format!("/w/{}", "a".repeat(300))})
            ),
            "a cut path"
        );
        assert!(
            !exact("Read", &json!({"file_path":"/etc/passwd"})),
            "shown as a bare basename"
        );
        assert!(
            exact("Read", &json!({"file_path":"/Users/me/.zshrc"})),
            "~/.zshrc is the whole path"
        );
        assert!(
            !exact("Grep", &json!({"pattern":"TODO","path":"/"})),
            "the searched tree is unseen"
        );
        assert!(exact(
            "WebFetch",
            &json!({"url":"https://example.com/a","prompt":"summarize"})
        ));
        assert!(!exact(
            "mcp__github__create_issue",
            &json!({"title":"t","body":"b"})
        ));
    }

    #[test]
    fn local_sessions_approval_shows_complete_native_mcp_arguments() {
        let list = "mcp__lightbulb_native_sessions__list_targets";
        let send = "mcp__lightbulb_native_sessions__send";
        let receipt = "mcp__lightbulb_native_sessions__receipt";
        assert!(exact(list, &json!({})));
        assert_eq!(display_summary(list, &json!({}), None, None), "list_targets {}");
        assert!(!exact(list, &json!({"scope":"all"})));

        let args = json!({"destination":"room:demo","text":"check","requestId":"req-1"});
        assert!(!exact(send, &args), "a private outbound text stays terminal-only");
        assert_eq!(display_summary(send, &args, None, None), claude::summary(send, &args, None, None));
        let receipt_args = json!({"requestId":"req-1"});
        assert!(exact(receipt, &receipt_args));
        assert_eq!(display_summary(receipt, &receipt_args, None, None), "receipt {\"requestId\":\"req-1\"}");
        assert!(!exact(receipt, &json!({"requestId":"req-1","extra":"hidden"})));
        assert!(!exact("mcp__lightbulb_native_sessions__send_more", &args));
        assert!(!exact(receipt, &json!({"requestId":"line 1\nline 2"})));
        assert!(!exact(receipt, &json!({"requestId":"caf\u{e9}"})));
        assert!(!exact(receipt, &json!({"requestId":"sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"})));
        assert!(!exact(receipt, &json!({"requestId":"x".repeat(201)})));
        assert!(modes(
            send,
            &json!([{"type":"setMode","mode":"acceptEdits","destination":"session"}])
        )
        .is_empty());
    }

    #[test]
    fn local_sessions_approval_offers_only_suggested_session_modes_and_never_for_shell() {
        let suggestions = json!([
            {"type":"setMode","mode":"acceptEdits","destination":"session"},
            {"type":"setMode","mode":"bypassPermissions","destination":"session"},
            {"type":"setMode","mode":"plan","destination":"localSettings"},
            {"type":"addRules","rules":[{"toolName":"Bash"}],"behavior":"allow","destination":"localSettings"}
        ]);
        assert_eq!(modes("Read", &suggestions), vec!["acceptEdits"]);
        assert!(modes("Bash", &suggestions).is_empty());
    }

    #[test]
    fn local_sessions_approval_digest_ignores_key_order_but_not_values() {
        assert_eq!(digest(&json!({"a":1,"b":2})), digest(&json!({"b":2,"a":1})));
        assert_ne!(
            digest(&json!({"command":"ls"})),
            digest(&json!({"command":"ls -a"}))
        );
    }

    #[test]
    fn local_sessions_approval_decisions_are_a_closed_set() {
        assert_eq!(
            decision(&answer("allow")).unwrap(),
            json!({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}})
        );
        let with_mode = Answer {
            mode: Some("acceptEdits".into()),
            ..answer("allow")
        };
        assert_eq!(
            decision(&with_mode).unwrap()["hookSpecificOutput"]["decision"]["updatedPermissions"],
            json!([{"type":"setMode","mode":"acceptEdits","destination":"session"}])
        );
        let deny = Answer {
            message: Some("not on main".into()),
            ..answer("deny")
        };
        assert_eq!(
            decision(&deny).unwrap()["hookSpecificOutput"]["decision"],
            json!({"behavior":"deny","message":"not on main"})
        );
        assert!(decision(&Answer {
            mode: Some("bypassPermissions".into()),
            ..answer("allow")
        })
        .is_none());
        assert!(decision(&Answer {
            mode: Some("plan".into()),
            ..answer("deny")
        })
        .is_none());
        assert!(decision(&answer("ask")).is_none());
        let text = decision(&answer("allow")).unwrap().to_string();
        assert!(
            !text.contains("updatedInput"),
            "an answer can never change what runs"
        );
    }

    #[test]
    fn local_sessions_approval_replies_only_into_the_private_hook_directory() {
        let dir = Path::new("/private/tmp/lightbulb-sessions-501");
        assert!(reply_allowed(
            dir,
            &format!("/private/tmp/lightbulb-sessions-501/{ID}.sock"),
            ID
        ));
        assert!(!reply_allowed(
            dir,
            &format!("/tmp/elsewhere/{ID}.sock"),
            ID
        ));
        assert!(!reply_allowed(
            dir,
            "/private/tmp/lightbulb-sessions-501/wake.sock",
            ID
        ));
        assert!(!reply_allowed(
            dir,
            &format!("/private/tmp/lightbulb-sessions-501/{ID}.sock"),
            "appr-XYZ"
        ));
        assert!(!valid_id("appr-0123456789ABCDEF0123456789abcdef"));
    }

    /// The whole hook half against a stand-in app: announce, be taken, get the
    /// exact answer, write one decision -- and ignore everything else on the way.
    #[test]
    fn local_sessions_approval_hook_writes_one_decision_for_the_exact_prompt() {
        let dir = PathBuf::from(format!(
            "/private/tmp/lbt-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"Bash",
            "tool_input":{"command":"git push"},"permission_suggestions":[]});
        let fd = {
            use std::os::unix::io::IntoRawFd;
            writer.into_raw_fd()
        };
        let wake_for_hook = wake.clone();
        let stdin_for_hook = stdin.clone();
        let hook = std::thread::spawn(move || hold(&wake_for_hook, "claude", &stdin_for_hook, fd));
        let mut buf = [0u8; 4096];
        let n = app.recv(&mut buf).unwrap();
        let event = lifecycle::parse_datagram(&buf[..n]).unwrap();
        let held = event.held.clone().expect("the prompt is announced as held");
        assert!(reply_allowed(&dir, &held.reply, &held.id));
        assert_eq!(held.input_digest, digest(&json!({"command":"git push"})));
        assert!(acknowledge(&held.reply, &held.id));
        // Noise the hook must ignore: a probe, a wrong id, a wrong digest.
        assert!(deliver(&held.reply, None));
        let wrong = Answer {
            approval_id: ID.into(),
            input_digest: held.input_digest.clone(),
            ..answer("allow")
        };
        assert!(deliver(&held.reply, Some(&wrong)));
        let stale = Answer {
            approval_id: held.id.clone(),
            ..answer("allow")
        };
        assert!(deliver(&held.reply, Some(&stale)));
        let right = Answer {
            approval_id: held.id.clone(),
            input_digest: held.input_digest.clone(),
            ..answer("allow")
        };
        assert!(deliver(&held.reply, Some(&right)));
        hook.join().unwrap();
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&out).unwrap(),
            json!({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}})
        );
        assert!(
            !Path::new(&held.reply).exists(),
            "the hook removes its channel"
        );
        assert!(
            !deliver(&held.reply, Some(&right)),
            "nobody holds an answered prompt"
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn local_sessions_approval_hook_lets_go_when_the_app_does_not_take_it() {
        let dir = PathBuf::from(format!(
            "/private/tmp/lbt-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let _app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let fd = {
            use std::os::unix::io::IntoRawFd;
            writer.into_raw_fd()
        };
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"Bash","tool_input":{"command":"ls"}});
        let started = Instant::now();
        hold(&wake, "claude", &stdin, fd);
        assert!(started.elapsed() < Duration::from_secs(5));
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert!(
            out.is_empty(),
            "no app, no answer: the terminal keeps the prompt"
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
