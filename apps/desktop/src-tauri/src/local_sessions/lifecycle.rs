//! Hook datagram parsing, the hook-event -> status transition table, and the
//! Codex hook-trust hash/key recipe. Pure module: no I/O, no wiring into the
//! transport yet (see Task 11).

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

pub(crate) const DATAGRAM_MAX: usize = 1500;
pub(crate) const LAST_MESSAGE_MAX: usize = 300;

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
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Transition {
    Start,
    Status(&'static str),
    Ended(&'static str),
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
/// order to stay under `DATAGRAM_MAX`: `lastAssistantMessage` -> `cwd` ->
/// `transcriptPath`.
pub(crate) fn build_datagram(provider: &str, stdin_json: &Value) -> Vec<u8> {
    let s = |k: &str| stdin_json[k].as_str().map(str::to_owned);
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
        ("transcriptPath", s("transcript_path")),
        ("cwd", s("cwd")),
        (
            "lastAssistantMessage",
            s("last_assistant_message").map(|m| m.chars().take(LAST_MESSAGE_MAX).collect()),
        ),
    ];
    let mut bytes = Vec::new();
    for drop in ["lastAssistantMessage", "cwd", "transcriptPath", ""] {
        let mut map = Map::new();
        for (k, v) in &fields {
            if let Some(v) = v {
                map.insert((*k).into(), if *k == "v" { json!(1) } else { json!(v) });
            }
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
    })
}

/// Map one hook event to the status/lifecycle transition the transport
/// applies (Task 11).
pub(crate) fn transition(event: &HookEvent) -> Transition {
    match event.event.as_str() {
        "SessionStart" => Transition::Start,
        "UserPromptSubmit" => Transition::Status("busy"),
        "Stop" => Transition::Status("idle"),
        "SessionEnd" => Transition::Ended("exited"),
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
fn canonical(v: &Value) -> Value {
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
        HookEvent { provider: Provider::Claude, event: event.into(), session_id: "s".into(), transcript_path: None, cwd: None, notification_type: None, message: None, reason: None, last_assistant_message: None }
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
        assert_eq!(transition(&ev("PreToolUse")), Transition::Ignore);
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
