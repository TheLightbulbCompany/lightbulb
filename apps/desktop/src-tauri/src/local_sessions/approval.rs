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
//!   session-scoped mode Claude itself suggested, allow + the allow rule Claude
//!   itself suggested ("similar"), pinned to this session, or deny. Never
//!   arbitrary `updatedInput`, never a persistent rule -- a remote approver cannot widen
//!   what runs beyond what Claude offered, nor past this session.
//! - AskUserQuestion has a separate answer contract: only answers are accepted,
//!   and the hook appends them to its original, digest-bound questions. Other
//!   tools can never receive rewritten input through this channel.
//! - A call can be approved remotely only when the approver saw all of it.
//!   `exact` (the legacy flag old clients gate on, never widened) says the
//!   one-line summary IS the call. `full` says the shipped input IS the raw
//!   call: uncut, unredacted, visible, at most `MAX_APPROVAL_INPUT` on the wire.
//!   That input travels hook -> app through the private 0600 side file (never
//!   the 1500-byte datagram), and the app re-checks its digest against the
//!   held one. Anything else is deny-only from Lightbulb and approved in the
//!   terminal.
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
/// Read-only tools whose one-line summary IS the thing being approved (the
/// legacy `exact` flag). An edit is not among them: its summary is the path, not
/// the content being written. Grep and Glob show a pattern but not the tree they
/// search. Those, and every tool whose summary is a sample of its input, can
/// still be approved remotely through `full`, when the whole input is shown.
const TARGET_TOOLS: [&str; 3] = ["Read", "WebFetch", "WebSearch"];
/// The most a remote approver is shown (ASCII-wire). Over it, or cut, or redacted, or with a
/// character a terminal would not show plainly, the call is not `full` and Allow stays on the Mac.
pub(crate) const MAX_APPROVAL_INPUT: usize = 10 * 1024;
/// The private side file's cap: the bounded input plus its id and digest.
const HELD_FILE_MAX: u64 = 16 * 1024;

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
    /// The input in the private side file is the whole raw call (see `shown_input`).
    #[serde(default)]
    pub full: bool,
    /// Claude's own suggested allow rule, as shown ("npm run *"): what "similar" allows for the
    /// rest of this session. The hook applies the rule from its own stdin, never from this text.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub similar: Option<String>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub answers: Option<Value>,
}

/// Hold permission prompts and complete questions through separate answer contracts.
pub(crate) fn holdable(provider: &str, stdin: &Value) -> bool {
    provider == "claude"
        && stdin["hook_event_name"] == "PermissionRequest"
        && stdin["tool_name"]
            .as_str()
            .is_some_and(|t| !t.is_empty() && (t != "AskUserQuestion" || question_relayable(&stdin["tool_input"])))
        && stdin["session_id"].as_str().is_some_and(|s| !s.is_empty())
}

/// Only bounded, complete Claude questions travel through the answer channel.
/// Duplicate question text is ambiguous because Claude's answers are keyed by that text.
pub(crate) fn question_input(input: &Value) -> bool {
    let Some(questions) = input["questions"].as_array() else { return false };
    if questions.is_empty() || questions.len() > 4 || !visible(input) { return false; }
    let mut texts = std::collections::HashSet::new();
    questions.iter().all(|q| {
        if !q.as_object().is_some_and(|o| o.keys().all(|k| matches!(k.as_str(), "question" | "header" | "options" | "multiSelect")))
            || !q.get("header").is_none_or(|h| h.as_str().is_some_and(|s| !s.trim().is_empty() && s.len() <= 2000)) { return false; }
        let Some(text) = q["question"].as_str().filter(|s| !s.trim().is_empty() && s.len() <= 2000) else { return false };
        let Some(options) = q["options"].as_array().filter(|o| !o.is_empty() && o.len() <= 9) else { return false };
        let mut labels = std::collections::HashSet::new();
        texts.insert(text) && q.get("multiSelect").is_none_or(Value::is_boolean) && options.iter().all(|o| {
            o.as_object().is_some_and(|o| o.keys().all(|k| matches!(k.as_str(), "label" | "description")))
                && o["label"].as_str().is_some_and(|s| !s.trim().is_empty() && s.len() <= 500 && labels.insert(s))
                && o.get("description").is_none_or(|d| d.as_str().is_some_and(|s| s.len() <= 2000 && (s.is_empty() || !s.trim().is_empty())))
        })
    }) && serde_json::to_vec(input).is_ok_and(|b| b.len() <= MAX_APPROVAL_INPUT)
        && question_answer_budget(questions)
}

/// Admission must leave enough answer space for every predefined selection and
/// at least a small custom answer. Question text is also serialized as map keys.
fn question_answer_budget(questions: &[Value]) -> bool {
    let mut worst = serde_json::Map::new();
    for q in questions {
        let labels: Vec<&str> = q["options"].as_array().unwrap().iter()
            .map(|o| o["label"].as_str().unwrap()).collect();
        let selectable = if q["multiSelect"] == true {
            labels.join(", ")
        } else {
            labels.into_iter().max_by_key(|s| serde_json::to_vec(s).unwrap().len()).unwrap().to_owned()
        };
        let custom = "x".repeat(128);
        let answer = if serde_json::to_vec(&selectable).unwrap().len() > serde_json::to_vec(&custom).unwrap().len() {
            selectable
        } else { custom };
        worst.insert(q["question"].as_str().unwrap().to_owned(), json!(answer));
    }
    serde_json::to_vec(&worst).is_ok_and(|b| b.len() <= 2000)
}

/// A question has no deny-only mode: if privacy/size filtering changes its input,
/// leave the terminal prompt alone instead of holding a question the app cannot show.
pub(crate) fn question_relayable(input: &Value) -> bool {
    question_input(input) && shown_input("AskUserQuestion", input).1
}

/// Answers may contain custom text, but can never replace questions or other tool arguments.
pub(crate) fn question_answers(input: &Value, answers: &Value) -> bool {
    if !question_input(input) { return false; }
    let Some(answers) = answers.as_object() else { return false };
    let questions = input["questions"].as_array().unwrap();
    answers.len() == questions.len() && questions.iter().all(|q| {
        answers.get(q["question"].as_str().unwrap()).and_then(Value::as_str)
            .is_some_and(|a| !a.trim().is_empty() && a.len() <= 2000 && !a.chars().any(hidden))
    }) && serde_json::to_vec(answers).is_ok_and(|b| b.len() <= 2000)
}

fn question_decision(input: &Value, answer: &Answer) -> Option<Value> {
    let answers = answer.answers.as_ref()?;
    if answer.decision != "answer" || answer.mode.is_some() || answer.message.is_some() || !question_answers(input, answers) { return None; }
    let mut updated = input.clone();
    updated["answers"] = answers.clone();
    Some(json!({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow","updatedInput":updated}}}))
}

pub(crate) fn digest(input: &Value) -> String {
    let bytes = serde_json::to_vec(&lifecycle::canonical(input)).unwrap_or_default();
    hex::encode(Sha256::digest(bytes))
}

/// The one-line reason a prompt shows. The held prompt's full input travels
/// separately, through the private side file, digest-bound (`shown_input`).
pub(crate) fn display_summary(tool: &str, input: &Value, cwd: Option<&str>, home: Option<&str>) -> String {
    // The public names reach here only when their arguments did not map.
    if matches!(tool, "mcp__lightbulb_native_sessions__send" | "mcp__lightbulb__send_message" | "mcp__lightbulb__message_receipt") {
        return "Lightbulb message".into();
    }
    if matches!(tool, "mcp__lightbulb__assign" | "mcp__lightbulb__cancel_assignment") {
        return "Lightbulb assignment".into();
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

/// A character a reader cannot see for what it is: Unicode Cc and Cf (except `\n`, `\t`), Zl,
/// Zp, and every Zs but U+0020 (a no-break space looks like a space and is a word character to
/// bash). The same set the web and phone clients refuse to approve. Cf and Zs as of Unicode 16.
fn hidden(c: char) -> bool {
    (c.is_control() && !matches!(c, '\n' | '\t'))
        || matches!(c,
            // Zs except U+0020, Zl, Zp
            '\u{00A0}' | '\u{1680}' | '\u{2000}'..='\u{200A}' | '\u{202F}' | '\u{205F}' | '\u{3000}'
            | '\u{2028}' | '\u{2029}'
            // Cf
            | '\u{00AD}' | '\u{0600}'..='\u{0605}' | '\u{061C}' | '\u{06DD}' | '\u{070F}'
            | '\u{0890}'..='\u{0891}' | '\u{08E2}' | '\u{180E}' | '\u{200B}'..='\u{200F}'
            | '\u{202A}'..='\u{202E}' | '\u{2060}'..='\u{2064}' | '\u{2066}'..='\u{206F}'
            | '\u{FEFF}' | '\u{FFF9}'..='\u{FFFB}' | '\u{110BD}' | '\u{110CD}'
            | '\u{13430}'..='\u{1343F}' | '\u{1BCA0}'..='\u{1BCA3}' | '\u{1D173}'..='\u{1D17A}'
            | '\u{E0001}' | '\u{E0020}'..='\u{E007F}')
}

fn visible(v: &Value) -> bool {
    match v {
        Value::String(s) => !s.chars().any(hidden),
        Value::Array(a) => a.iter().all(visible),
        Value::Object(o) => o.iter().all(|(k, v)| !k.chars().any(hidden) && visible(v)),
        _ => true,
    }
}

/// What a remote approver is shown, and whether it is the whole raw call (then its digest equals
/// `digest(raw)` and a client that hashes what it displayed can prove it saw all of it). A call
/// that names a secret anywhere (`SECRET_NAMES`, a whole `auth` segment, in a key or a value) or
/// carries URL userinfo (`scheme://user:pw@`) is never full: `redact` is a
/// backstop, so a call that touches credentials is approved at the terminal.
pub(crate) fn shown_input(tool: &str, raw: &Value) -> (Value, bool) {
    static USERINFO: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(super::URL_USERINFO).expect("userinfo pattern"));
    if tool.starts_with("mcp__lightbulb") {
        return (json!({}), false); // private paths keep their own rules
    }
    let (shown, cut) = super::bound_json(raw.clone(), MAX_APPROVAL_INPUT);
    let full = !cut
        && shown.as_object().is_some_and(|o| !o.is_empty())
        && shown == *raw
        && visible(&shown)
        && super::ascii_wire_len(&shown).is_ok_and(|n| n <= MAX_APPROVAL_INPUT)
        && !super::names_secret(&raw.to_string())
        && !USERINFO.is_match(&raw.to_string());
    (shown, full)
}

fn private_send_args(input: &Value) -> bool {
    // The retired app MCP's internal send shape: exactly destination, text and requestId.
    if !input.as_object().is_some_and(|args| args.len() == 3) {
        return false;
    }
    let destination = input["destination"].as_str().unwrap_or("");
    let body = input["text"].as_str().unwrap_or("");
    let request = input["requestId"].as_str().unwrap_or("");
    destination.len() <= 256
        && ["room:", "person:", "session:", "agent:"]
            .iter()
            .any(|prefix| destination.starts_with(prefix))
        && !body.trim().is_empty()
        && body.len() <= 4096
        && uuid::Uuid::parse_str(request).is_ok()
}

/// The held prompt's side file, read once and removed: only from the private
/// hook directory, ours, 0600, bounded, and naming this approval and digest.
pub(crate) fn read_held_input(held: &Held) -> Option<Value> {
    let dir = Path::new(&held.reply).parent()?;
    if !reply_allowed(dir, &held.reply, &held.id) || private_dir(dir).is_err() {
        return None;
    }
    let path = dir.join(format!("{}.json", held.id));
    let meta = fs::symlink_metadata(&path).ok()?;
    if !meta.is_file()
        || meta.uid() != unsafe { libc::geteuid() }
        || meta.mode() & 0o077 != 0
        || meta.len() > HELD_FILE_MAX
    {
        return None;
    }
    let bytes = fs::read(&path).ok()?;
    let _ = fs::remove_file(&path);
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    (value["approvalId"] == held.id && value["inputDigest"] == held.input_digest).then_some(value)
}

/// A native send's exact outbound message, as the owner's private approval detail.
pub(crate) fn private_send(value: &Value, held: &Held) -> Option<Value> {
    let args = json!({"destination":value["destination"],"text":value["text"],"requestId":value["requestId"]});
    (private_send_args(&args) && digest(&args) == held.input_digest).then(|| {
        json!({"approvalId":held.id,"inputDigest":held.input_digest,
            "destination":args["destination"],"text":args["text"],"requestId":args["requestId"]})
    })
}

/// The input a remote approver is shown, and whether it is the whole call. `full` is the hook's
/// verdict re-checked here: the shown input must hash to the held digest.
pub(crate) fn held_input(tool: &str, value: &Value, held: &Held) -> (Option<Value>, bool) {
    if tool.starts_with("mcp__lightbulb") {
        return (None, false);
    }
    let input = value.get("input").filter(|i| i.is_object()).cloned();
    let full = held.full && input.as_ref().is_some_and(|i| digest(i) == held.input_digest);
    (input, full)
}

/// The session-scoped mode switches Claude itself offered for this prompt. A
/// shell prompt offers none: "approve this command" must not widen into "and
/// everything after it".
pub(crate) fn modes(tool: &str, suggestions: &Value) -> Vec<String> {
    if tool == "Bash" || tool.starts_with("mcp__lightbulb_native_sessions__") || tool.starts_with("mcp__lightbulb__") {
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

/// The longest suggested rule a remote approver is offered (it rides the held datagram).
const SIMILAR_MAX: usize = 120;

/// The allow rule Claude itself suggested for this prompt (the one its own dialog offers as
/// "Yes, and don't ask again for ..."), and its text as an approver sees it. Only rules for
/// this prompt's own tool, with plain visible content, and never for Lightbulb's own tools.
/// Claude suggests it for `localSettings`; the decision pins it to the session (verified on
/// 2.1.311: a `destination: "session"` rule from this hook stops the next matching prompt and
/// writes no settings file).
pub(crate) fn similar(tool: &str, suggestions: &Value) -> Option<(Value, String)> {
    if tool.starts_with("mcp__lightbulb") {
        return None;
    }
    let suggestion = suggestions.as_array()?.iter().find(|s| s["type"] == "addRules" && s["behavior"] == "allow")?;
    let rules = suggestion["rules"].as_array().filter(|r| !r.is_empty() && r.len() <= 4)?;
    let mut shown = vec![];
    for rule in rules {
        let content = rule["ruleContent"].as_str().filter(|c| !c.trim().is_empty())?;
        if rule["toolName"] != tool || rule.as_object()?.len() != 2 {
            return None;
        }
        shown.push(content);
    }
    let text = shown.join(", ");
    (text.len() <= SIMILAR_MAX && text.bytes().all(|b| (0x20..0x7f).contains(&b)))
        .then(|| (Value::Array(rules.clone()), text))
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

/// The hook's stdout document for one delivered answer. `similar` is the rule the hook's own
/// stdin suggested (`similar()`), never anything the app sent.
pub(crate) fn decision(answer: &Answer, similar: Option<&Value>) -> Option<Value> {
    if answer.answers.is_some() { return None; }
    let decision = match answer.decision.as_str() {
        "allow" => {
            let mut d = json!({"behavior":"allow"});
            match answer.mode.as_deref() {
                None => {}
                Some("similar") => {
                    d["updatedPermissions"] =
                        json!([{"type":"addRules","rules":similar?,"behavior":"allow","destination":"session"}]);
                }
                Some(mode) if MODES.contains(&mode) => {
                    d["updatedPermissions"] =
                        json!([{"type":"setMode","mode":mode,"destination":"session"}]);
                }
                Some(_) => return None,
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
    let private_path = dir.join(format!("{id}.json"));
    let native_send = tool == "mcp__lightbulb_native_sessions__send";
    let (shown, full) = shown_input(tool, &stdin["tool_input"]);
    let private_payload = if native_send {
        json!({
            "approvalId": id,
            "inputDigest": digest(&stdin["tool_input"]),
            "destination": stdin["tool_input"]["destination"],
            "text": stdin["tool_input"]["text"],
            "requestId": stdin["tool_input"]["requestId"],
        })
    } else {
        json!({"approvalId": id, "inputDigest": digest(&stdin["tool_input"]), "input": shown})
    };
    // Only through this 0600 file: the input can be far larger than the datagram.
    let written = (!native_send || private_send_args(&stdin["tool_input"]))
        && serde_json::to_vec(&private_payload).is_ok_and(|bytes| bytes.len() as u64 <= HELD_FILE_MAX)
        && private_write(&private_path, &private_payload).is_ok();
    let private_send = native_send && written;
    let _private_guard = Remove(private_path);
    if tool == "AskUserQuestion" && (!full || !written || !question_input(&stdin["tool_input"])) { return; }
    let similar = similar(tool, &stdin["permission_suggestions"]);
    let mut modes = modes(tool, &stdin["permission_suggestions"]);
    if similar.is_some() {
        modes.push("similar".into());
    }
    let held = Held {
        id: id.clone(),
        input_digest: digest(&stdin["tool_input"]),
        reply: path.to_string_lossy().into_owned(),
        exact: (tool != "mcp__lightbulb_native_sessions__send" || private_send)
            && exact(
                tool,
                &stdin["tool_input"],
                stdin["cwd"].as_str(),
                std::env::var("HOME").ok().as_deref(),
            ),
        modes,
        full: full && written,
        similar: similar.as_ref().map(|(_, text)| text.clone()),
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
        // The id names this one prompt (and so its tool); the digest, its raw input.
        if answer.decision == "allow" && !(held.exact || held.full) {
            continue;
        }
        if answer
            .mode
            .as_ref()
            .is_some_and(|m| !held.modes.contains(m))
        {
            continue;
        }
        let document = if tool == "AskUserQuestion" {
            if !held.full { continue; }
            question_decision(&stdin["tool_input"], &answer)
        } else {
            decision(&answer, similar.as_ref().map(|(rules, _)| rules))
        };
        let Some(document) = document else {
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
            answers: None,
        }
    }

    fn question() -> Value {
        json!({"questions":[{"question":"Which framework?","header":"Framework","options":[{"label":"React","description":"Components"},{"label":"Vue"}],"multiSelect":false}]})
    }

    #[test]
    fn local_sessions_question_contract_is_narrow_and_preserves_original_input() {
        let input = question();
        let mut a = answer("answer");
        a.answers = Some(json!({"Which framework?":"React"}));
        let document = question_decision(&input, &a).unwrap();
        let updated = &document["hookSpecificOutput"]["decision"]["updatedInput"];
        assert_eq!(updated["questions"], input["questions"]);
        assert_eq!(updated["answers"], a.answers.clone().unwrap());
        assert_eq!(document["hookSpecificOutput"]["hookEventName"], "PermissionRequest");
        assert!(decision(&a, None).is_none(), "ordinary permissions never accept answers");
        assert!(question_decision(&input, &answer("allow")).is_none(), "allow alone never answers a question");
        assert!(serde_json::from_value::<Answer>(json!({"approvalId":ID,"inputDigest":"d".repeat(64),"decision":"answer","updatedInput":{"command":"evil"}})).is_err());
        assert!(!question_answers(&input, &json!({"Other question":"React"})));
        assert!(!question_answers(&input, &json!({"Which framework?":"React","Extra":"x"})));
        assert!(!question_answers(&input, &json!({"Which framework?":"é".repeat(1000)})));
        a.mode = Some("acceptEdits".into());
        assert!(question_decision(&input, &a).is_none());
        let mut duplicate = input.clone();
        duplicate["questions"].as_array_mut().unwrap().push(input["questions"][0].clone());
        assert!(!question_input(&duplicate));
        let mut optional = input.clone();
        optional["questions"][0].as_object_mut().unwrap().remove("multiSelect");
        assert!(question_input(&optional), "omitted multiSelect means single select");
        optional["questions"][0]["multiSelect"] = json!("false");
        assert!(!question_input(&optional));
        let hook = json!({"hook_event_name":"PermissionRequest","tool_name":"AskUserQuestion","session_id":"s","tool_input":input});
        assert!(holdable("claude", &hook));
        let mut passive = hook; passive["hook_event_name"] = json!("PreToolUse");
        assert!(!holdable("claude", &passive), "passive discovery cannot block the interactive picker");
    }

    #[test]
    fn local_sessions_question_admission_reserves_answer_keys_selections_and_custom_budget() {
        assert!(question_relayable(&question()));
        let long_keys = json!({"questions":(0..4).map(|n| json!({"question":format!("{n}{}","q".repeat(549)),"options":[{"label":"A"},{"label":"B"}]})).collect::<Vec<_>>()});
        assert!(!question_input(&long_keys), "four 550-byte question keys cannot fit an answer");
        let multi = json!({"questions":[{"question":"Pick?","multiSelect":true,"options":(0..9).map(|n| json!({"label":format!("{n}{}","a".repeat(259))})).collect::<Vec<_>>()}]});
        assert!(!question_input(&multi), "all predefined multi-select labels must fit together");
        let escaped = json!({"questions":(0..4).map(|n| json!({"question":format!("Pick {n}?"),"options":[{"label":"\\".repeat(300)}]})).collect::<Vec<_>>()});
        assert!(!question_input(&escaped), "answer admission counts JSON escaping, not raw label bytes");
        let tight_custom = json!({"questions":(0..4).map(|n| json!({"question":format!("{n}{}","q".repeat(399)),"options":[{"label":"A"}]})).collect::<Vec<_>>()});
        assert!(!question_input(&tight_custom), "short predefined options must still leave custom-answer space");
        for input in [long_keys,multi,escaped,tight_custom] {
            assert!(!holdable("claude", &json!({"hook_event_name":"PermissionRequest","tool_name":"AskUserQuestion","session_id":"s","tool_input":input})));
        }
    }

    #[test]
    fn local_sessions_question_unrepresentable_inputs_never_enter_the_hold() {
        let mut secret = question();
        secret["questions"][0]["question"] = json!("Which API_KEY should be used?");
        assert!(question_input(&secret), "question schema alone is insufficient");
        assert!(!shown_input("AskUserQuestion", &secret).1);
        assert!(!question_relayable(&secret), "privacy filtering must leave the terminal in charge");
        let mut unicode = question();
        for option in unicode["questions"][0]["options"].as_array_mut().unwrap() {
            option["description"] = json!("é".repeat(1000));
        }
        assert!(question_input(&unicode));
        assert!(!shown_input("AskUserQuestion", &unicode).1, "ASCII wire expansion exceeds the relay bound");
        for input in [secret,unicode] {
            let hook = json!({"hook_event_name":"PermissionRequest","tool_name":"AskUserQuestion","session_id":"s","tool_input":input});
            assert!(!holdable("claude", &hook));
        }
        assert!(holdable("claude", &json!({"hook_event_name":"PermissionRequest","tool_name":"Bash","session_id":"s","tool_input":{"command":"echo API_KEY"}})), "ordinary permissions keep deny-only holds");
        for header in [json!(42),json!("   "),json!(null)] {
            let mut input = question(); input["questions"][0]["header"] = header;
            assert!(!question_relayable(&input));
        }
        let mut extra = question(); extra["questions"][0]["unknown"] = json!(true);
        assert!(!question_relayable(&extra));
        let mut extra = question(); extra["questions"][0]["options"][0]["unknown"] = json!(true);
        assert!(!question_relayable(&extra));
        for description in [json!(42),json!("  "),json!(null)] {
            let mut input = question(); input["questions"][0]["options"][0]["description"] = description;
            assert!(!question_relayable(&input));
        }
        let mut empty = question(); empty["questions"][0]["options"][0]["description"] = json!("");
        assert!(question_relayable(&empty));
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
    fn local_sessions_approval_keeps_engine_tool_arguments_off_the_shared_prompt() {
        // The engine's `lightbulb` tools: a send never shows its text, and none is relayed as exact.
        let send = "mcp__lightbulb__send_message";
        let args = json!({"text":"secret","request_id":"x","room_id":"r1"});
        assert_eq!(display_summary(send, &args, None, None), "Lightbulb message");
        for t in ["mcp__lightbulb__assign", "mcp__lightbulb__cancel_assignment"] {
            assert_eq!(display_summary(t, &args, None, None), "Lightbulb assignment");
        }
        assert!(!exact(send, &args));
        assert!(!exact("mcp__lightbulb__find_rooms", &json!({})));
        assert!(!exact("mcp__lightbulb_native_sessions__send", &json!({"destination":"room:demo","text":"check","requestId":"e12fd3e8-32ca-4b65-8eaf-ab979e3c9449"})));
        assert!(modes(send, &json!([{"type":"setMode","mode":"acceptEdits","destination":"session"}])).is_empty());
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
    fn local_sessions_approval_similar_is_only_claudes_own_suggested_rule_for_this_tool() {
        // As Claude Code 2.1.311 suggests it for `npm run greet`.
        let npm = json!([{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"npm run *"}],"behavior":"allow","destination":"localSettings"}]);
        let (rules, text) = similar("Bash", &npm).unwrap();
        assert_eq!(rules, json!([{"toolName":"Bash","ruleContent":"npm run *"}]));
        assert_eq!(text, "npm run *");
        assert!(similar("Read", &npm).is_none(), "a rule for another tool");
        assert!(similar("Bash", &json!([{"type":"setMode","mode":"acceptEdits","destination":"session"}])).is_none());
        assert!(similar("Bash", &json!([{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"x"}],"behavior":"deny","destination":"session"}])).is_none());
        assert!(similar("Bash", &json!([{"type":"addRules","rules":[{"toolName":"Bash"}],"behavior":"allow"}])).is_none(), "a whole-tool rule");
        assert!(similar("Bash", &json!([{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"rm \u{202e}x"}],"behavior":"allow"}])).is_none());
        assert!(similar("Bash", &json!([{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"x".repeat(200)}],"behavior":"allow"}])).is_none());
        let send = json!([{"type":"addRules","rules":[{"toolName":"mcp__lightbulb__send_message","ruleContent":"x"}],"behavior":"allow"}]);
        assert!(similar("mcp__lightbulb__send_message", &send).is_none());
    }

    /// "Allow similar" through the whole hook: the app sends only the mode; the rule written to
    /// Claude is the one the hook's own stdin suggested, pinned to the session.
    #[test]
    fn local_sessions_approval_hook_allows_similar_with_its_own_suggested_rule() {
        let dir = PathBuf::from(format!("/private/tmp/lbt-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"Bash",
            "tool_input":{"command":"npm run greet"},
            "permission_suggestions":[{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"npm run *"}],"behavior":"allow","destination":"localSettings"}]});
        let fd = {
            use std::os::unix::io::IntoRawFd;
            writer.into_raw_fd()
        };
        let (w, st) = (wake.clone(), stdin.clone());
        let hook = std::thread::spawn(move || hold(&w, "claude", &st, fd));
        let mut buf = [0u8; 4096];
        let n = app.recv(&mut buf).unwrap();
        let held = lifecycle::parse_datagram(&buf[..n]).unwrap().held.unwrap();
        assert_eq!(held.modes, vec!["similar"]);
        assert_eq!(held.similar.as_deref(), Some("npm run *"));
        assert!(acknowledge(&held.reply, &held.id));
        let allow = Answer { approval_id: held.id.clone(), input_digest: held.input_digest.clone(), ..answer("allow") };
        assert!(deliver(&held.reply, Some(&Answer { mode: Some("acceptEdits".into()), ..allow.clone() })), "not offered: ignored");
        assert!(deliver(&held.reply, Some(&Answer { mode: Some("similar".into()), ..allow })));
        hook.join().unwrap();
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&out).unwrap()["hookSpecificOutput"]["decision"]["updatedPermissions"],
            json!([{"type":"addRules","rules":[{"toolName":"Bash","ruleContent":"npm run *"}],"behavior":"allow","destination":"session"}])
        );
        let _ = fs::remove_dir_all(&dir);
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
    fn local_sessions_private_send_file_is_bound_to_the_held_input() {
        let dir = tempfile::tempdir().unwrap();
        let scope = dir.path().join("private");
        private_dir(&scope).unwrap();
        let id = format!("appr-{}", uuid::Uuid::new_v4().simple());
        let args = json!({"destination":"room:demo","text":"private\nmessage","requestId":uuid::Uuid::new_v4().to_string()});
        let held = Held {
            id: id.clone(),
            input_digest: digest(&args),
            reply: scope.join(format!("{id}.sock")).to_string_lossy().into_owned(),
            exact: true,
            modes: vec![], full: false, similar: None,
        };
        let path = scope.join(format!("{id}.json"));
        let detail = json!({"approvalId":id,"inputDigest":held.input_digest,
            "destination":args["destination"],"text":args["text"],"requestId":args["requestId"]});
        private_write(&path, &detail).unwrap();
        let read = read_held_input(&held).unwrap();
        assert_eq!(private_send(&read, &held), Some(detail.clone()));
        assert!(!path.exists());
        // A file naming another prompt, or loose permissions, is refused.
        private_write(&path, &json!({"approvalId":ID,"inputDigest":held.input_digest})).unwrap();
        assert!(read_held_input(&held).is_none());
        private_write(&path, &detail).unwrap();
        fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o644)).unwrap();
        assert!(read_held_input(&held).is_none());
    }

    #[test]
    fn local_sessions_approval_shown_input_is_full_only_when_it_is_the_whole_visible_call() {
        let (s, full) = shown_input("Bash", &json!({"command": "ls\nls -la"}));
        assert!(full);
        assert_eq!(s["command"], "ls\nls -la");
        let (_, full) = shown_input("Edit", &json!({"file_path": "/Users/a/x", "old_string": "a", "new_string": "b"}));
        assert!(full);
        let (s, full) = shown_input("Write", &json!({"file_path": "/x", "content": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}));
        assert!(!full);
        assert!(s["content"].as_str().unwrap().contains("[redacted]"));
        for content in [r#"{"password": "$Pa55word"}"#, "password: $Secret1\n", "PASSWORD=${PREFIX}hunter2\n", "password=hunter2(\n",
            "password=f(x)hunter2\n", "password: |\n  hunter2\n", r#"{"password": "abc\"hunter2"}"#] {
            let (s, full) = shown_input("Write", &json!({"file_path": "/x/config", "content": content}));
            assert!(!full, "a quoted or mixed-case `$` secret is redacted: {s}");
            assert!(s["content"].as_str().unwrap().contains("[redacted]"));
        }
        // A call that names a secret is approved at the terminal, even when nothing was redacted.
        for kept in ["Authorization: Bearer $TOKEN", "X_API_KEY=$X_API_KEY", "echo $AUTH", "cat ~/.cookies"] {
            let (s, full) = shown_input("Bash", &json!({"command": kept}));
            assert!(!full && s["command"] == kept, "{kept}");
        }
        assert!(!shown_input("Bash", &json!({"command": "ls", "Token": 1})).1, "keys are checked too");
        let (s, full) = shown_input("Bash", &json!({"command": "git push https://bob:pw@host/r"}));
        assert!(!full && s["command"] == "git push https://bob:[redacted]@host/r", "userinfo");
        assert!(shown_input("Bash", &json!({"command": "echo author oauth OAuth authentic AUTHOR https://host/a:b@c"})).1);
        for raw in super::super::tests::KEYED {
            let (s, full) = shown_input("mcp__x__call", &serde_json::from_str(raw).unwrap());
            assert!(!full && !s.to_string().contains("hunter2"), "{s}");
        }
        assert!(!shown_input("mcp__x__call", &json!({})).1, "an empty input is never full");
        for leak in super::super::tests::LEAKS {
            let (s, full) = shown_input("Write", &json!({"file_path": "/x/config", "content": leak}));
            assert!(!full && !s.to_string().contains("hunter2"), "{s}");
        }
        let (_, full) = shown_input("Write", &json!({"content": "x".repeat(20 * 1024)}));
        assert!(!full, "cut");
        let (_, full) = shown_input("Write", &json!({"content": "\u{e9}".repeat(2 * 1024)}));
        assert!(!full, "fits as UTF-8, not on the ASCII wire");
        let (_, full) = shown_input("Bash", &json!({"command": "ls\u{202e}"}));
        assert!(!full, "bidi");
        let (_, full) = shown_input("Bash", &json!({"command": "ls\r"}));
        assert!(!full, "carriage return");
        let (_, full) = shown_input("Bash", &json!({"command": "rm\u{00a0}-rf /tmp/x"}));
        assert!(!full, "a no-break space reads as a space and is not one");
        for hidden in ["\u{200b}", "\u{2028}", "\u{3000}", "\u{00ad}", "\u{e0041}", "\u{feff}"] {
            assert!(!shown_input("Bash", &json!({"command": format!("ls{hidden}")})).1, "{hidden:?}");
        }
        assert!(!shown_input("Bash", &json!({"command": "ls", "x\u{200b}": 1})).1, "keys are shown too");
        assert!(shown_input("Bash", &json!({"command": "echo caf\u{e9}\tok"})).1, "plain non-ASCII is visible");
        let (s, full) = shown_input("mcp__lightbulb__send_message", &json!({"text": "hi"}));
        assert!(!full);
        assert_eq!(s, json!({}));
        let (_, full) = shown_input("mcp__lightbulb_native_sessions__send", &json!({"text": "hi"}));
        assert!(!full);
        // Full means the digest of what was shown IS the held digest.
        let raw = json!({"file_path": "/x", "old_string": "a", "new_string": "b"});
        assert_eq!(digest(&shown_input("Edit", &raw).0), digest(&raw));
        // ...and `exact` is not widened by any of it.
        assert!(!exact("Edit", &raw));
    }

    #[test]
    fn local_sessions_approval_digest_matches_the_clients_canonical_json() {
        // packages/shared localSessionTranscript.test.ts pins the same vector for canonicalJson.
        assert_eq!(
            digest(&json!({"description":"d","command":"ls"})),
            "b1ff34673e7684fa23bbcb26c95bae8dbdb6490caa1455565e88a5b55200813e"
        );
    }

    #[test]
    fn local_sessions_held_input_is_full_only_when_it_hashes_to_the_held_digest() {
        let raw = json!({"file_path":"/x","old_string":"a","new_string":"b"});
        let held = Held { id: ID.into(), input_digest: digest(&raw), reply: String::new(), exact: false, modes: vec![], full: true, similar: None };
        let file = json!({"approvalId": ID, "inputDigest": held.input_digest, "input": raw});
        assert_eq!(held_input("Edit", &file, &held), (Some(raw.clone()), true));
        let redacted = json!({"approvalId": ID, "inputDigest": held.input_digest, "input": {"file_path":"/x","old_string":"[redacted]","new_string":"b"}});
        assert!(!held_input("Edit", &redacted, &held).1, "a datagram's `full` is re-checked against the digest");
        assert!(!held_input("Edit", &file, &Held { full: false, ..held.clone() }).1);
        assert_eq!(held_input("mcp__lightbulb__find_rooms", &file, &held), (None, false));
    }

    #[test]
    fn local_sessions_approval_decisions_are_a_closed_set() {
        assert_eq!(
            decision(&answer("allow"), None).unwrap(),
            json!({"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}})
        );
        let with_mode = Answer {
            mode: Some("acceptEdits".into()),
            ..answer("allow")
        };
        assert_eq!(
            decision(&with_mode, None).unwrap()["hookSpecificOutput"]["decision"]["updatedPermissions"],
            json!([{"type":"setMode","mode":"acceptEdits","destination":"session"}])
        );
        let deny = Answer {
            message: Some("not on main".into()),
            ..answer("deny")
        };
        assert_eq!(
            decision(&deny, None).unwrap()["hookSpecificOutput"]["decision"],
            json!({"behavior":"deny","message":"not on main"})
        );
        assert!(decision(&Answer {
            mode: Some("bypassPermissions".into()),
            ..answer("allow")
        }, None)
        .is_none());
        assert!(decision(&Answer {
            mode: Some("plan".into()),
            ..answer("deny")
        }, None)
        .is_none());
        let similar = Answer { mode: Some("similar".into()), ..answer("allow") };
        assert!(decision(&similar, None).is_none(), "no suggested rule, no similar");
        let rules = json!([{"toolName":"Bash","ruleContent":"npm run *"}]);
        assert_eq!(
            decision(&similar, Some(&rules)).unwrap()["hookSpecificOutput"]["decision"],
            json!({"behavior":"allow","updatedPermissions":[{"type":"addRules","rules":rules,"behavior":"allow","destination":"session"}]}),
            "the rule Claude suggested, pinned to this session"
        );
        assert!(decision(&Answer { mode: Some("similar".into()), ..answer("deny") }, Some(&rules)).is_none());
        assert!(decision(&answer("ask"), None).is_none());
        let text = decision(&answer("allow"), None).unwrap().to_string();
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
    fn local_sessions_question_hook_writes_one_bound_answer_and_closes() {
        let dir = PathBuf::from(format!(
            "/private/tmp/lbt-{}",
            &uuid::Uuid::new_v4().simple().to_string()[..8]
        ));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"AskUserQuestion",
            "tool_input":question(),"permission_suggestions":[]});
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
        assert_eq!(held.input_digest, digest(&question()));
        assert!(acknowledge(&held.reply, &held.id));
        // Noise the hook must ignore: a probe, a wrong id, a wrong digest.
        assert!(deliver(&held.reply, None));
        let wrong = Answer {
            approval_id: ID.into(),
            input_digest: held.input_digest.clone(),
            answers: Some(json!({"Which framework?":"React"})),
            ..answer("answer")
        };
        assert!(deliver(&held.reply, Some(&wrong)));
        let stale = Answer {
            approval_id: held.id.clone(),
            answers: Some(json!({"Which framework?":"React"})),
            ..answer("answer")
        };
        assert!(deliver(&held.reply, Some(&stale)));
        let right = Answer {
            approval_id: held.id.clone(),
            input_digest: held.input_digest.clone(),
            answers: Some(json!({"Which framework?":"React"})),
            ..answer("answer")
        };
        assert!(deliver(&held.reply, Some(&right)));
        hook.join().unwrap();
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&out).unwrap(),
            question_decision(&question(), &right).unwrap()
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

    /// An edit is never `exact`, but shipped whole it is `full`: the input reaches the app only
    /// through the private side file, and the hook takes an allow for it.
    #[test]
    fn local_sessions_approval_hook_allows_a_full_call_through_its_side_file() {
        let dir = PathBuf::from(format!("/private/tmp/lbt-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let input = json!({"file_path":"/w/a.rs","old_string":"x".repeat(3000),"new_string":"y"});
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"Edit",
            "tool_input":input,"permission_suggestions":[]});
        let fd = {
            use std::os::unix::io::IntoRawFd;
            writer.into_raw_fd()
        };
        let (w, st) = (wake.clone(), stdin.clone());
        let hook = std::thread::spawn(move || hold(&w, "claude", &st, fd));
        let mut buf = [0u8; 4096];
        let n = app.recv(&mut buf).unwrap();
        assert!(n <= 1500, "the datagram never carries the input");
        assert!(!String::from_utf8_lossy(&buf[..n]).contains("xxxx"));
        let held = lifecycle::parse_datagram(&buf[..n]).unwrap().held.unwrap();
        assert!(!held.exact && held.full);
        let file = read_held_input(&held).expect("the side file");
        assert_eq!(held_input("Edit", &file, &held), (Some(input.clone()), true));
        assert!(acknowledge(&held.reply, &held.id));
        assert!(deliver(&held.reply, Some(&Answer { approval_id: held.id.clone(), input_digest: held.input_digest.clone(), ..answer("allow") })));
        hook.join().unwrap();
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&out).unwrap()["hookSpecificOutput"]["decision"], json!({"behavior":"allow"}));
        let _ = fs::remove_dir_all(&dir);
    }

    /// A redacted call is neither exact nor full: the hook ignores an allow and takes a deny.
    #[test]
    fn local_sessions_approval_hook_refuses_an_allow_for_a_call_not_shown_whole() {
        let dir = PathBuf::from(format!("/private/tmp/lbt-{}", &uuid::Uuid::new_v4().simple().to_string()[..8]));
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        let wake = dir.join("wake.sock");
        let app = UnixDatagram::bind(&wake).unwrap();
        let (mut reader, writer) = std::os::unix::net::UnixStream::pair().unwrap();
        let stdin = json!({"hook_event_name":"PermissionRequest","session_id":"s","tool_name":"Write",
            "tool_input":{"file_path":"/w/.env","content":"ghp_abcdefghijklmnopqrstuvwxyz0123456789"}});
        let fd = {
            use std::os::unix::io::IntoRawFd;
            writer.into_raw_fd()
        };
        let (w, st) = (wake.clone(), stdin.clone());
        let hook = std::thread::spawn(move || hold(&w, "claude", &st, fd));
        let mut buf = [0u8; 4096];
        let n = app.recv(&mut buf).unwrap();
        let held = lifecycle::parse_datagram(&buf[..n]).unwrap().held.unwrap();
        assert!(!held.exact && !held.full);
        let file = read_held_input(&held).unwrap();
        assert!(file["input"]["content"].as_str().unwrap().contains("[redacted]"));
        assert!(acknowledge(&held.reply, &held.id));
        let allow = Answer { approval_id: held.id.clone(), input_digest: held.input_digest.clone(), ..answer("allow") };
        assert!(deliver(&held.reply, Some(&allow)));
        assert!(deliver(&held.reply, Some(&Answer { decision: "deny".into(), ..allow })));
        hook.join().unwrap();
        let mut out = String::new();
        std::io::Read::read_to_string(&mut reader, &mut out).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&out).unwrap()["hookSpecificOutput"]["decision"]["behavior"], "deny");
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
