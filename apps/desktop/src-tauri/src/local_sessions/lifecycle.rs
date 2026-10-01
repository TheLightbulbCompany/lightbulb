//! Hook datagram parsing, the hook-event -> status transition table, and the
//! Codex hook-trust hash/key recipe. Pure module: no I/O, no wiring into the
//! transport yet (see Task 11).

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub(crate) const DATAGRAM_MAX: usize = 1500;
pub(crate) const LAST_MESSAGE_MAX: usize = 300;
/// One short line: the question a session is asking, or the call it wants
/// permission for. Never the raw `tool_input`, which is unbounded and can hold
/// file contents or a secret.
pub(crate) const WAITING_TEXT_MAX: usize = 200;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Provider {
    Claude,
    Codex,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HookEvent {
    pub provider: Provider,
    pub event: String, // hook_event_name as sent
    pub session_id: String,
    pub transcript_path: Option<String>,
    pub cwd: Option<String>,
    pub notification_type: Option<String>,
    pub message: Option<String>, // Notification message (for the "permission" fallback)
    pub reason: Option<String>,
    pub last_assistant_message: Option<String>,
    pub tool_name: Option<String>,
    /// Identity of the pending tool call, when the event carries one. Claude
    /// 2.1.273 puts `tool_use_id` in a `PreToolUse` hook's stdin but NOT in a
    /// `PermissionRequest`'s (whose input is `hook_event_name` + `tool_name` +
    /// `tool_input` + `permission_suggestions` and the common session fields),
    /// so this is present for a question and absent for a permission prompt --
    /// callers must have a fallback. Parsed either way, so the day it appears
    /// on both, dedupe gets sharper with no change here.
    pub tool_use_id: Option<String>,
    /// A short human summary of the tool's input -- the rendering the mirrored
    /// transcript already uses. Never the input itself.
    pub waiting_text: Option<String>,
    /// A permission prompt the hook is holding open for a remote answer.
    pub held: Option<super::approval::Held>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Transition {
    Start,
    Status(&'static str),
    /// Waiting on the human at the keyboard, with the reason to show when the
    /// hook carried one. Same status as `Status("waiting-approval")`, plus a
    /// transcript row -- Claude writes neither a pending question nor a
    /// permission prompt to its transcript, so this is the only record of it.
    Waiting {
        text: Option<String>,
    },
    Ended(&'static str),
    /// The hook let a held prompt go (it timed out); the prompt stays in the terminal.
    Release(String),
    Ignore,
}

/// Take the longest leading prefix of `s` whose UTF-8 encoding is at most
/// `max` bytes, never splitting a multi-byte character.
fn take_bytes(s: &str, max: usize) -> String {
    let mut end = 0;
    for (i, c) in s.char_indices() {
        let next = i + c.len_utf8();
        if next > max {
            break;
        }
        end = next;
    }
    s[..end].to_string()
}

/// Build the wire datagram sent to the local relay for one hook invocation.
/// Every non-droppable field (`event`, `sessionId`, `reason`,
/// `notificationType`, `message`) is byte-bounded up front via
/// [`take_bytes`] since they come from untrusted hook stdin and a
/// multi-byte-character payload could otherwise blow past its char-count cap
/// in bytes; `lastAssistantMessage` keeps its char-based cap because it is
/// droppable. On top of that, drops the largest/least essential fields in
/// order to stay under `DATAGRAM_MAX`: `lastAssistantMessage` -> `waitingText`
/// -> `cwd` -> `transcriptPath`.
pub(crate) fn build_datagram(provider: &str, stdin_json: &Value) -> Vec<u8> {
    build(provider, stdin_json, None)
}

/// A held prompt's announcement: the ordinary datagram plus how to answer it.
/// The approval fields are never dropped for size -- without them the prompt
/// cannot be answered, and each is bounded (an id, a digest, a short path).
pub(crate) fn build_held_datagram(
    provider: &str,
    stdin_json: &Value,
    held: &super::approval::Held,
) -> Vec<u8> {
    build(provider, stdin_json, Some(held))
}

/// The hook let a held prompt go.
pub(crate) fn build_release_datagram(provider: &str, stdin_json: &Value, id: &str) -> Vec<u8> {
    let session: String = stdin_json["session_id"].as_str().unwrap_or("").chars().take(128).collect();
    serde_json::to_vec(&json!({"v":1,"provider":provider,"event":"PermissionRelease","sessionId":session,"approvalId":id}))
        .unwrap_or_default()
}

fn build(provider: &str, stdin_json: &Value, held: Option<&super::approval::Held>) -> Vec<u8> {
    let s = |k: &str| stdin_json[k].as_str().map(str::to_owned);
    // Summarized, never forwarded: `tool_input` is unbounded and holds file
    // contents, patches and whatever a command line carries.
    let waiting = stdin_json["tool_name"].as_str().map(|name| {
        let input = &stdin_json["tool_input"];
        let cwd = stdin_json["cwd"].as_str();
        let home = std::env::var("HOME").ok();
        match held {
            Some(_) => super::approval::display_summary(name, input, cwd, home.as_deref()),
            None => super::claude::summary(name, input, cwd, home.as_deref()),
        }
    });
    let mut fields: Vec<(&str, Option<String>)> = vec![
        ("v", Some("1".into())),
        ("provider", Some(provider.into())),
        ("event", s("hook_event_name").map(|m| take_bytes(&m, 32))),
        ("sessionId", s("session_id").map(|m| take_bytes(&m, 128))),
        ("reason", s("reason").map(|m| take_bytes(&m, 64))),
        (
            "notificationType",
            s("notification_type").map(|m| take_bytes(&m, 64)),
        ),
        ("message", s("message").map(|m| take_bytes(&m, 120))),
        ("toolName", s("tool_name").map(|m| take_bytes(&m, 64))),
        ("toolUseId", s("tool_use_id").map(|m| take_bytes(&m, 128))),
        (
            "waitingText",
            waiting
                .filter(|t| !t.is_empty())
                .map(|t| take_bytes(&t, WAITING_TEXT_MAX)),
        ),
        ("transcriptPath", s("transcript_path")),
        ("cwd", s("cwd")),
        (
            "lastAssistantMessage",
            s("last_assistant_message").map(|m| m.chars().take(LAST_MESSAGE_MAX).collect()),
        ),
    ];
    let mut bytes = Vec::new();
    for drop in [
        "lastAssistantMessage",
        "waitingText",
        "cwd",
        "transcriptPath",
        "",
    ] {
        let mut map = Map::new();
        for (k, v) in &fields {
            if let Some(v) = v {
                map.insert((*k).into(), if *k == "v" { json!(1) } else { json!(v) });
            }
        }
        if let Some(held) = held {
            map.insert("held".into(), json!(held));
        }
        bytes = serde_json::to_vec(&Value::Object(map)).unwrap_or_default();
        if bytes.len() <= DATAGRAM_MAX || drop.is_empty() {
            break;
        }
        for (k, v) in fields.iter_mut() {
            if *k == drop {
                *v = None;
            }
        }
    }
    bytes
}

/// Parse a datagram produced by [`build_datagram`]. Returns `None` for the
/// plain-wake byte (`b"1"`) and for anything that isn't a well-formed event
/// (garbage, or missing a non-empty, bounded `sessionId`).
pub(crate) fn parse_datagram(bytes: &[u8]) -> Option<HookEvent> {
    let v: Value = serde_json::from_slice(bytes).ok()?;
    let provider = match v["provider"].as_str()? {
        "claude" => Provider::Claude,
        "codex" => Provider::Codex,
        _ => return None,
    };
    let s = |k: &str| v[k].as_str().map(str::to_owned);
    Some(HookEvent {
        provider,
        event: s("event")?,
        session_id: s("sessionId").filter(|x| !x.is_empty() && x.len() <= 128)?,
        transcript_path: s("transcriptPath"),
        cwd: s("cwd"),
        notification_type: s("notificationType"),
        message: s("message"),
        reason: s("reason"),
        // Bounded on the way in, not just on the way out: anything same-uid can
        // write to `wake.sock`, and an oversized value would be mirrored into
        // every device heartbeat, which the relay rejects whole.
        last_assistant_message: s("lastAssistantMessage")
            .map(|m| m.chars().take(LAST_MESSAGE_MAX).collect()),
        tool_name: s("toolName").map(|m| take_bytes(&m, 64)),
        tool_use_id: s("toolUseId").map(|m| take_bytes(&m, 128)),
        waiting_text: s("waitingText").map(|m| take_bytes(&m, WAITING_TEXT_MAX)),
        // Anything same-uid can write here; `approval::reply_allowed` and the
        // hook's own id/digest check are what make a held prompt trustworthy.
        held: serde_json::from_value::<super::approval::Held>(v["held"].clone())
            .ok()
            .filter(|h| {
                super::approval::valid_id(&h.id)
                    && h.input_digest.len() == 64
                    && h.input_digest.bytes().all(|b| b.is_ascii_hexdigit())
                    && h.reply.len() <= 256
                    && h.modes.len() <= 3
            })
            .or_else(|| {
                // A release names only the prompt it lets go.
                (s("event").as_deref() == Some("PermissionRelease"))
                    .then(|| s("approvalId"))
                    .flatten()
                    .filter(|id| super::approval::valid_id(id))
                    .map(|id| super::approval::Held {
                        id,
                        input_digest: String::new(),
                        reply: String::new(),
                        exact: false,
                        modes: vec![],
                    })
            }),
    })
}

/// The one line a reader sees for a waiting session: the question as asked
/// (`Tea or coffee? (Tea / Coffee)`), or the call a permission prompt is about
/// (`Wants to run Bash(git push)`) -- a permission prompt needs the frame, a
/// question is already one.
///
/// `AskUserQuestion` reaches here down both paths. Whichever of its two hooks a
/// reader sees first, it reads as the question: "Wants to run
/// AskUserQuestion(Tea or coffee?)" is not how anyone asks one.
fn waiting_reason(event: &HookEvent) -> Option<String> {
    let summary = event.waiting_text.as_deref().unwrap_or("").trim();
    if event.event != "PermissionRequest" || event.tool_name.as_deref() == Some("AskUserQuestion") {
        return (!summary.is_empty()).then(|| summary.to_owned());
    }
    let call = match event.tool_name.as_deref().unwrap_or("").trim() {
        "" => summary.to_owned(),
        tool if summary.is_empty() => tool.to_owned(),
        tool => format!("{tool}({summary})"),
    };
    (!call.is_empty()).then(|| take_bytes(&format!("Wants to run {call}"), WAITING_TEXT_MAX))
}

/// Map one hook event to the status/lifecycle transition the transport
/// applies (Task 11).
pub(crate) fn transition(event: &HookEvent) -> Transition {
    match event.event.as_str() {
        "SessionStart" => Transition::Start,
        "UserPromptSubmit" => Transition::Status("busy"),
        "Stop" => Transition::Status("idle"),
        "SessionEnd" => Transition::Ended("exited"),
        "PermissionRelease" => match &event.held {
            Some(held) => Transition::Release(held.id.clone()),
            None => Transition::Ignore,
        },
        // The installed matcher already scopes this to AskUserQuestion; a
        // settings file left behind by an older build must not flood us with
        // every tool call.
        "PreToolUse" if event.tool_name.as_deref() == Some("AskUserQuestion") => {
            Transition::Waiting {
                text: waiting_reason(event),
            }
        }
        // A question fires BOTH hooks, in no guaranteed order and with no
        // guarantee both arrive. Neither is dropped -- either alone must still
        // show the prompt -- so they are worded alike here and paired into one
        // row by the transport.
        "PermissionRequest" => Transition::Waiting {
            text: waiting_reason(event),
        },
        "Notification" => match event.notification_type.as_deref() {
            Some("permission_prompt") => Transition::Status("waiting-approval"),
            Some("idle_prompt") => Transition::Status("idle"),
            Some(_) => Transition::Ignore,
            None if event
                .message
                .as_deref()
                .is_some_and(|m| m.to_ascii_lowercase().contains("permission")) =>
            {
                Transition::Status("waiting-approval")
            }
            None => Transition::Ignore,
        },
        _ => Transition::Ignore,
    }
}

/// Recursively sort object keys so JSON serialization is deterministic
/// regardless of insertion order (this crate's `serde_json` has
/// `preserve_order`, so `Map` does NOT sort on its own).
pub(crate) fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut keys: Vec<_> = m.keys().cloned().collect();
            keys.sort();
            Value::Object(keys.into_iter().map(|k| (k.clone(), canonical(&m[&k]))).collect())
        }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        other => other.clone(),
    }
}

/// The Codex hook-trust hash: identity object -> recursively key-sorted ->
/// compact `serde_json::to_vec` -> SHA-256 -> `sha256:<lowercase hex>`.
pub(crate) fn codex_trust_hash(
    event_label: &str,
    matcher: Option<&str>,
    command: &str,
    timeout: u64,
    r#async: bool,
    status_message: Option<&str>,
) -> String {
    let mut hook = json!({"type":"command","command":command,"timeout":timeout,"async":r#async});
    if let Some(s) = status_message {
        hook["statusMessage"] = json!(s);
    }
    let mut ident = json!({"event_name":event_label,"hooks":[hook]});
    if let Some(m) = matcher {
        ident["matcher"] = json!(m);
    }
    let bytes = serde_json::to_vec(&canonical(&ident)).unwrap_or_default();
    format!("sha256:{}", hex::encode(Sha256::digest(bytes)))
}

/// The `~/.codex/config.toml` trust-table key for one hook group.
pub(crate) fn codex_trust_key(
    hooks_json: &std::path::Path,
    event_label: &str,
    group_index: usize,
) -> String {
    format!("{}:{}:{}:0", hooks_json.display(), event_label, group_index)
}

/// Claude Code hook event name -> Codex hook-trust event label.
pub(crate) fn codex_event_label(event: &str) -> Option<&'static str> {
    Some(match event {
        "SessionStart" => "session_start",
        "UserPromptSubmit" => "user_prompt_submit",
        "Stop" => "stop",
        "SessionEnd" => "session_end",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(event: &str) -> HookEvent {
        HookEvent { provider: Provider::Claude, event: event.into(), session_id: "s".into(), transcript_path: None, cwd: None, notification_type: None, message: None, reason: None, last_assistant_message: None, tool_name: None, tool_use_id: None, waiting_text: None, held: None }
    }

    #[test]
    fn lifecycle_transition_table() {
        assert_eq!(transition(&ev("SessionStart")), Transition::Start);
        assert_eq!(transition(&ev("UserPromptSubmit")), Transition::Status("busy"));
        assert_eq!(transition(&ev("Stop")), Transition::Status("idle"));
        assert_eq!(transition(&HookEvent { notification_type: Some("permission_prompt".into()), ..ev("Notification") }), Transition::Status("waiting-approval"));
        assert_eq!(transition(&HookEvent { notification_type: Some("idle_prompt".into()), ..ev("Notification") }), Transition::Status("idle"));
        assert_eq!(transition(&HookEvent { notification_type: Some("auth_success".into()), ..ev("Notification") }), Transition::Ignore);
        assert_eq!(transition(&HookEvent { message: Some("Claude needs your permission to run Bash".into()), ..ev("Notification") }), Transition::Status("waiting-approval"));
        assert_eq!(transition(&HookEvent { message: Some("Done".into()), ..ev("Notification") }), Transition::Ignore);
        assert_eq!(transition(&ev("SessionEnd")), Transition::Ended("exited"));
    }

    #[test]
    fn lifecycle_waiting_transition_needs_a_question_or_a_permission_prompt() {
        let waiting = |text: &str| Transition::Waiting { text: Some(text.into()) };
        let question = HookEvent {
            tool_name: Some("AskUserQuestion".into()),
            waiting_text: Some("Tea or coffee? (Tea / Coffee)".into()),
            ..ev("PreToolUse")
        };
        assert_eq!(transition(&question), waiting("Tea or coffee? (Tea / Coffee)"));
        // Every other tool call is ordinary work, and the transcript carries it.
        assert_eq!(
            transition(&HookEvent { tool_name: Some("Bash".into()), ..question.clone() }),
            Transition::Ignore,
            "a stale settings file must not report every tool call as waiting"
        );
        assert_eq!(transition(&ev("PreToolUse")), Transition::Ignore);

        let permission = HookEvent {
            tool_name: Some("Bash".into()),
            waiting_text: Some("git push".into()),
            ..ev("PermissionRequest")
        };
        assert_eq!(transition(&permission), waiting("Wants to run Bash(git push)"));
        assert_eq!(
            transition(&HookEvent { waiting_text: None, ..permission.clone() }),
            waiting("Wants to run Bash")
        );
        assert_eq!(
            transition(&ev("PermissionRequest")),
            Transition::Waiting { text: None },
            "still a waiting row: the status is the point, the reason is a bonus"
        );
        assert_eq!(
            transition(&HookEvent {
                tool_name: Some("AskUserQuestion".into()),
                waiting_text: Some("Tea or coffee? (Tea / Coffee)".into()),
                ..permission.clone()
            }),
            waiting("Tea or coffee? (Tea / Coffee)"),
            "a question reads as a question down either of its two hooks"
        );
        // The prefix is part of the bound, not on top of it.
        let long = HookEvent { waiting_text: Some("x".repeat(WAITING_TEXT_MAX)), ..permission };
        let Transition::Waiting { text: Some(text) } = transition(&long) else {
            panic!("expected a waiting transition");
        };
        assert_eq!(text.len(), WAITING_TEXT_MAX);
    }

    #[test]
    fn lifecycle_datagram_roundtrip_and_size_fallback() {
        let stdin = json!({"session_id":"abc","transcript_path":"/t/x.jsonl","cwd":"/w","hook_event_name":"Stop","last_assistant_message":"x".repeat(1000)});
        let bytes = build_datagram("claude", &stdin);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.provider, Provider::Claude);
        assert_eq!(parsed.session_id, "abc");
        assert_eq!(parsed.last_assistant_message.as_deref().map(str::len), Some(LAST_MESSAGE_MAX));
        // 1600 (not 1400): transcriptPath alone must still exceed DATAGRAM_MAX
        // after cwd is dropped, so the fallback actually reaches its third
        // drop instead of returning early with transcriptPath retained.
        let long = json!({"session_id":"abc","transcript_path":"/".repeat(1600),"cwd":"/".repeat(200),"hook_event_name":"SessionStart"});
        let bytes = build_datagram("codex", &long);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.provider, Provider::Codex);
        assert!(parsed.transcript_path.is_none() && parsed.cwd.is_none());
        assert!(parse_datagram(b"1").is_none());
        assert!(parse_datagram(b"{not json").is_none());
        assert!(parse_datagram(br#"{"v":1,"provider":"claude","event":"Stop"}"#).is_none(), "missing sessionId is a wake, not an event");
    }

    #[test]
    fn lifecycle_datagram_carries_a_bounded_tool_summary_never_the_input() {
        let stdin = json!({"session_id":"abc","hook_event_name":"PreToolUse","tool_name":"AskUserQuestion",
            "tool_use_id":"toolu_01aaaa",
            "tool_input":{"questions":[{"question":"Tea or coffee?","options":[{"label":"Tea"},{"label":"Coffee"}]}]}});
        let parsed = parse_datagram(&build_datagram("claude", &stdin)).unwrap();
        assert_eq!(parsed.tool_name.as_deref(), Some("AskUserQuestion"));
        assert_eq!(
            parsed.waiting_text.as_deref(),
            Some("Tea or coffee? (Tea / Coffee)")
        );
        // A question carries the pending call's identity; a permission prompt
        // does not send one (verified against Claude 2.1.273's hook input).
        assert_eq!(parsed.tool_use_id.as_deref(), Some("toolu_01aaaa"));
        assert_eq!(
            parse_datagram(&build_datagram(
                "claude",
                &json!({"session_id":"abc","hook_event_name":"PermissionRequest","tool_name":"Bash"})
            ))
            .unwrap()
            .tool_use_id,
            None
        );

        // What a tool is asked to write never rides along -- only the summary.
        let stdin = json!({"session_id":"abc","hook_event_name":"PermissionRequest","cwd":"/w",
            "tool_name":"Write","tool_input":{"file_path":"/w/src/main.rs","content":"SECRET".repeat(500)}});
        let bytes = build_datagram("claude", &stdin);
        assert!(!String::from_utf8_lossy(&bytes).contains("SECRET"));
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.waiting_text.as_deref(), Some("src/main.rs"));

        let stdin = json!({"session_id":"abc","hook_event_name":"PermissionRequest",
            "tool_name":"mcp__lightbulb_native_sessions__receipt",
            "tool_input":{"requestId":"req-1"}});
        let ordinary = parse_datagram(&build_datagram("claude", &stdin)).unwrap();
        assert_eq!(ordinary.waiting_text.as_deref(), Some("req-1"));
        let held = super::super::approval::Held {
            id: "appr-0123456789abcdef0123456789abcdef".into(),
            input_digest: "d".repeat(64), reply: "/tmp/appr.sock".into(),
            exact: true, modes: vec![],
        };
        let prompt = parse_datagram(&build_held_datagram("claude", &stdin, &held)).unwrap();
        assert_eq!(prompt.waiting_text.as_deref(), Some("receipt {\"requestId\":\"req-1\"}"));

        // A long command line is cut to the bound, and a tool with nothing to
        // summarize sends no empty field at all.
        let stdin = json!({"session_id":"abc","hook_event_name":"PermissionRequest",
            "tool_name":"Bash","tool_input":{"command":"echo ".to_owned() + &"a".repeat(4000)}});
        let bytes = build_datagram("claude", &stdin);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.waiting_text.map(|t| t.len()), Some(WAITING_TEXT_MAX));
        let stdin =
            json!({"session_id":"abc","hook_event_name":"PermissionRequest","tool_name":"Bash","tool_input":{}});
        assert!(parse_datagram(&build_datagram("claude", &stdin))
            .unwrap()
            .waiting_text
            .is_none());

        // Droppable, ahead of cwd and transcriptPath: an oversized datagram
        // keeps the fields the transport cannot work without.
        let stdin = json!({"session_id":"abc","hook_event_name":"PermissionRequest","cwd":"/w",
            "transcript_path":"/".repeat(1600),"tool_name":"Bash","tool_input":{"command":"git push"}});
        let bytes = build_datagram("claude", &stdin);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.tool_name.as_deref(), Some("Bash"));
        assert!(parsed.waiting_text.is_none());

        // A datagram this process did not build is bounded on the way in too.
        let hostile = json!({"v":1,"provider":"claude","event":"PermissionRequest","sessionId":"abc",
            "toolName":"B".repeat(500),"waitingText":"c".repeat(4000)})
        .to_string();
        let parsed = parse_datagram(hostile.as_bytes()).unwrap();
        assert_eq!(parsed.tool_name.map(|t| t.len()), Some(64));
        assert_eq!(parsed.waiting_text.map(|t| t.len()), Some(WAITING_TEXT_MAX));
    }

    #[test]
    fn lifecycle_datagram_bounds_oversized_multibyte_untrusted_fields() {
        let emoji = "\u{1F600}".repeat(2000); // 4 bytes per char in UTF-8
        let stdin = json!({
            "session_id": emoji.clone(),
            "hook_event_name": emoji.clone(),
            "reason": emoji.clone(),
            "notification_type": emoji.clone(),
            "message": emoji.clone(),
            "transcript_path": "/t/x.jsonl",
            "cwd": "/w",
            "last_assistant_message": "x".repeat(2000),
        });
        let bytes = build_datagram("claude", &stdin);
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert!(parsed.session_id.len() <= 128);
        assert!(parsed.event.len() <= 32);

        // A datagram this process did not build: same-uid, so the parse side is
        // the only bound there is.
        let hostile = json!({"v":1,"provider":"claude","event":"Stop","sessionId":"abc",
            "lastAssistantMessage":"y".repeat(2000)})
        .to_string();
        let parsed = parse_datagram(hostile.as_bytes()).unwrap();
        assert_eq!(
            parsed.last_assistant_message.unwrap().chars().count(),
            LAST_MESSAGE_MAX
        );
    }

    #[test]
    fn lifecycle_take_bytes_never_splits_a_multibyte_char() {
        assert_eq!(take_bytes("\u{1F600}\u{1F600}", 5), "\u{1F600}");
        assert_eq!(take_bytes("\u{1F600}\u{1F600}", 8), "\u{1F600}\u{1F600}");
        assert_eq!(take_bytes("\u{1F600}\u{1F600}", 3), "");
        assert_eq!(take_bytes("hello", 3), "hel");
    }

    #[test]
    fn lifecycle_codex_trust_hash_matches_live_entries() {
        assert_eq!(codex_trust_hash("session_start", Some("startup|resume|clear|compact"), "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-activate.js\"", 5, false, Some("Loading ponytail mode...")),
            "sha256:5f81d38f47448a1581c08ec877e044d9e04dd6f814dce3f2671f7a8edadd719b");
        assert_eq!(codex_trust_hash("user_prompt_submit", None, "node \"${CLAUDE_PLUGIN_ROOT}/hooks/ponytail-mode-tracker.js\"", 5, false, Some("Tracking ponytail mode...")),
            "sha256:6a6f42bc3b58d6262db38bfd74d7f340fcca2b09cdb134aad365063f0bfefca4");
        assert_eq!(codex_trust_hash("session_end", Some(""), "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/cleanup.sh\"", 1, false, None),
            "sha256:08cef4005eb5f328882260c2e8cb44a5d31551a0db0d04273fb07ce8e39cbfc9");
        assert_eq!(codex_trust_key(std::path::Path::new("/Users/me/.codex/hooks.json"), "stop", 2), "/Users/me/.codex/hooks.json:stop:2:0");
        assert_eq!(codex_event_label("UserPromptSubmit"), Some("user_prompt_submit"));
        assert_eq!(codex_event_label("Notification"), None);
    }
}
