//! Hook datagram parsing, the hook-event -> status transition table, and the
//! Codex hook-trust hash/key recipe. Hook sources come from OS process ancestry.

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
    /// Cursor and Grok CLI: no session registry, no push channel. A session exists from its
    /// first hook and hears a message at its next one (`super::hooked`).
    Cursor,
    Grok,
}

impl Provider {
    pub(crate) fn as_str(&self) -> &'static str {
        match self {
            Provider::Claude => "claude",
            Provider::Codex => "codex",
            Provider::Cursor => "cursor",
            Provider::Grok => "grok",
        }
    }

    pub(crate) fn parse(name: &str) -> Option<Self> {
        Some(match name {
            "claude" => Provider::Claude,
            "codex" => Provider::Codex,
            "cursor" => Provider::Cursor,
            "grok" => Provider::Grok,
            _ => return None,
        })
    }
}

/// Native process identity, independent of executable aliases and relay generations.
/// Fences process restarts, not reordered turns within one process. A Codex
/// app server can host multiple conversations under the same PID and birth time.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(crate) struct HookProcess {
    pub pid: u32,
    pub birth_micros: u64,
}

impl HookProcess {
    pub(super) fn of(process: &super::Process) -> Self {
        Self { pid: process.pid, birth_micros: process.birth_micros }
    }
}

/// Only shells may intervene between the hook and its native provider. Do not
/// walk through an unknown launcher to some unrelated provider farther up.
/// Claude's native installer also exposes releases as `claude/versions/<v>`;
/// its app bundle hard link is named `claude`. Codex Desktop, daemon and held
/// CLI processes all run the native `codex` executable.
fn hook_ancestor(
    provider: &str,
    mut current: super::Process,
    mut parent: impl FnMut(&super::Process) -> Option<super::Process>,
) -> Option<HookProcess> {
    if !matches!(provider, "claude" | "codex" | "cursor" | "grok") { return None; }
    let hooked = super::hooked::provider(provider);
    let mut seen = std::collections::BTreeSet::new();
    for _ in 0..8 {
        if current.pid <= 1 || current.birth_micros == 0 || !seen.insert(current.pid) {
            return None;
        }
        let path = &current.executable;
        let name = path.file_name()?.to_str()?;
        if (hooked && hooked_agent(provider, path)) || name == provider || (provider == "claude" && crate::holder::run::is_claude_program(path)) {
            return Some(HookProcess::of(&current));
        }
        if !matches!(name, "sh" | "bash" | "zsh" | "dash") { return None; }
        current = parent(&current)?;
    }
    None
}

/// Whether `path` is the executable of the hooked agent `provider` (Cursor or Grok CLI). The
/// nearest non-shell ancestor is not enough: `local-session-hook grok …` run from a shell inside a
/// Claude session made that Claude a Grok session (prod e2e 2026-10-22), and its own local MCP
/// then saw two sessions on one pid. Cursor's agent runs as `cursor-agent/versions/<v>/node` (CLI)
/// or inside `Cursor.app` (the IDE's helper); Grok CLI as `grok` or `grok-native`. A Claude or
/// Codex executable is never one, wherever it is installed.
pub(super) fn hooked_agent(provider: &str, path: &std::path::Path) -> bool {
    let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
    if native_agent(path) {
        return false;
    }
    let component = |c: &str| path.components().any(|p| p.as_os_str() == c);
    match provider {
        "cursor" => name == "cursor-agent" || component("cursor-agent") || component("Cursor.app"),
        "grok" => matches!(name, "grok" | "grok-native"),
        _ => false,
    }
}

/// A Claude Code or Codex executable: a session of its own, never a Cursor or Grok record's.
pub(super) fn native_agent(path: &std::path::Path) -> bool {
    matches!(path.file_name().and_then(|n| n.to_str()), Some("claude" | "codex")) || crate::holder::run::is_claude_program(path)
}

/// Resolve a relationship, not just two live PIDs. A reparented child keeps
/// its birth time, and a reused parent PID may already name another process.
fn checked_parent(
    child: &super::Process,
    mut parent: impl FnMut(u32) -> Option<super::Process>,
    mut process: impl FnMut(u32) -> Option<super::Process>,
) -> Option<super::Process> {
    let first = parent(child.pid)?;
    if first.birth_micros > child.birth_micros
        || parent(child.pid)? != first
        || process(child.pid)? != *child
    {
        return None;
    }
    Some(first)
}

pub(super) fn hook_process(provider: &str) -> Option<HookProcess> {
    let own = super::process(std::process::id()).ok()?;
    let parent = |child: &super::Process| checked_parent(
        child,
        |pid| super::native_tools::parent(pid).ok(),
        |pid| super::process(pid).ok(),
    );
    let first = parent(&own)?;
    let source = hook_ancestor(provider, first, parent)?;
    let live = super::process(source.pid).ok()?;
    (HookProcess::of(&live) == source).then_some(source)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct HookEvent {
    pub provider: Provider,
    pub source_process: Option<HookProcess>,
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
    build(provider, stdin_json, None, hook_process(provider))
}

/// A held prompt's announcement: the ordinary datagram plus how to answer it.
/// The approval fields are never dropped for size -- without them the prompt
/// cannot be answered, and each is bounded (an id, a digest, a short path).
pub(crate) fn build_held_datagram(
    provider: &str,
    stdin_json: &Value,
    held: &super::approval::Held,
) -> Vec<u8> {
    build(provider, stdin_json, Some(held), hook_process(provider))
}

/// The hook let a held prompt go.
pub(crate) fn build_release_datagram(provider: &str, stdin_json: &Value, id: &str) -> Vec<u8> {
    let session: String = stdin_json["session_id"].as_str().unwrap_or("").chars().take(128).collect();
    serde_json::to_vec(&json!({"v":2,"provider":provider,"event":"PermissionRelease","sessionId":session,"approvalId":id,"sourceProcess":hook_process(provider)}))
        .unwrap_or_default()
}

fn build(provider: &str, stdin_json: &Value, held: Option<&super::approval::Held>, source: Option<HookProcess>) -> Vec<u8> {
    let s = |k: &str| stdin_json[k].as_str().map(str::to_owned);
    // Summarized, never forwarded: `tool_input` is unbounded and holds file
    // contents, patches and whatever a command line carries. A held prompt's
    // bounded, redacted input goes through the private side file instead
    // (`approval::hold`), digest-bound; this datagram carries only the summary.
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
        ("v", Some("2".into())),
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
                map.insert((*k).into(), if *k == "v" { json!(2) } else { json!(v) });
            }
        }
        map.insert("sourceProcess".into(), json!(source));
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
    // Legacy datagrams still wake discovery, but cannot identify a process.
    // Only v2 attributes a hook to a process. A v1 datagram carrying a
    // non-null identity is rejected, never upgraded into a lifecycle mutation.
    // Unknown versions and malformed identities must not degrade to legacy.
    match v["v"].as_u64()? {
        1 if !v["sourceProcess"].is_null() => return None,
        1 | 2 => {},
        _ => return None,
    }
    let source_process = if v["sourceProcess"].is_null() {
        None
    } else {
        let source: HookProcess = serde_json::from_value(v["sourceProcess"].clone()).ok()?;
        if source.pid <= 1 || source.pid > i32::MAX as u32 || source.birth_micros == 0 {
            return None;
        }
        Some(source)
    };
    let provider = Provider::parse(v["provider"].as_str()?)?;
    let s = |k: &str| v[k].as_str().map(str::to_owned);
    Some(HookEvent {
        provider,
        source_process,
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
                    && h.modes.len() <= 4
                    && h.similar.as_ref().is_none_or(|s| s.len() <= 120)
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
                        modes: vec![], full: false, similar: None,
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

/// A mirror's end reason for a session its person ended on purpose. On the wire only for a relay
/// that lists `endedOnPurpose` (`session_states::end_reason`).
pub(crate) const ON_PURPOSE: &str = "on-purpose";
/// The app ended a held session whose holder is older than this macOS login session
/// (`holder::manager::stranded`): its provider had lost DNS and the keychain for good.
pub(crate) const LOGIN_SESSION_ENDED: &str = "login-session-ended";
/// The app stopped a held session left idle with nobody on it (`transport::Host::stop_idle`).
pub(crate) const IDLE_STOPPED: &str = "idle-stopped";

/// Map one hook event to the status/lifecycle transition the transport
/// applies (Task 11).
pub(crate) fn transition(event: &HookEvent) -> Transition {
    match event.event.as_str() {
        "SessionStart" => Transition::Start,
        "UserPromptSubmit" => Transition::Status("busy"),
        "Stop" => Transition::Status("idle"),
        // Claude names why it ends: the person's own exit (`/exit`, ctrl+d) or `/logout` is an end
        // on purpose. A hang-up, a kill or a shutdown says "other"; Codex names nothing.
        "SessionEnd" if matches!(event.reason.as_deref(), Some("prompt_input_exit" | "logout")) => Transition::Ended(ON_PURPOSE),
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
        "PostToolUse" => "post_tool_use",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ev(event: &str) -> HookEvent {
        HookEvent { source_process: None, provider: Provider::Claude, event: event.into(), session_id: "s".into(), transcript_path: None, cwd: None, notification_type: None, message: None, reason: None, last_assistant_message: None, tool_name: None, tool_use_id: None, waiting_text: None, held: None }
    }

    fn ancestor(pid: u32, executable: &str) -> super::super::Process {
        super::super::Process { pid, birth_micros: 100 + pid as u64, executable: executable.into() }
    }

    #[test]
    fn lifecycle_parent_rejects_a_reparented_child() {
        let child = ancestor(100, "/hook");
        let old = ancestor(10, "/opt/codex");
        let adopted = ancestor(11, "/other/parent");
        let mut reads = [old, adopted].into_iter();
        assert_eq!(checked_parent(&child, |_| reads.next(), |_| Some(child.clone())), None);
    }

    #[test]
    fn lifecycle_parent_rejects_a_parent_born_after_the_child() {
        let child = ancestor(100, "/hook");
        let reused = ancestor(200, "/opt/codex");
        assert_eq!(checked_parent(&child, |_| Some(reused.clone()), |_| Some(child.clone())), None);
    }

    #[test]
    fn lifecycle_parent_rejects_parent_pid_reuse_during_resolution() {
        let child = ancestor(100, "/hook");
        let old = ancestor(10, "/opt/codex");
        let mut reused = old.clone();
        reused.birth_micros += 1;
        let mut reads = [old, reused].into_iter();
        assert_eq!(checked_parent(&child, |_| reads.next(), |_| Some(child.clone())), None);
    }

    #[test]
    fn lifecycle_parent_rejects_child_pid_reuse_during_resolution() {
        let child = ancestor(100, "/hook");
        let parent = ancestor(10, "/opt/codex");
        let mut reused = child.clone();
        reused.birth_micros += 1;
        assert_eq!(checked_parent(&child, |_| Some(parent.clone()), |_| Some(reused.clone())), None);
    }

    #[test]
    fn lifecycle_parent_accepts_only_a_stable_older_relationship() {
        let child = ancestor(100, "/hook");
        let parent = ancestor(10, "/opt/codex");
        assert_eq!(checked_parent(&child, |_| Some(parent.clone()), |_| Some(child.clone())), Some(parent.clone()));
        let mut vanished = [Some(parent), None].into_iter();
        assert_eq!(checked_parent(&child, |_| vanished.next().flatten(), |_| Some(child.clone())), None);
    }

    #[test]
    fn lifecycle_hook_generation_follows_native_ancestry_only() {
        for (provider, path) in [
            ("claude", "/opt/claude"),
            ("claude", "/home/.local/share/claude/versions/2.1.273"),
            ("claude", "/opt/ClaudeCode.app/Contents/MacOS/claude"),
            ("codex", "/Applications/Codex.app/Contents/Resources/codex"),
            ("codex", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
            ("codex", "/home/.codex/packages/app-server-daemon/releases/0.159.2-arm64-apple-darwin/bin/codex"),
        ] {
            let native = ancestor(42, path);
            let expected = Some(HookProcess::of(&native));
            assert_eq!(hook_ancestor(provider, native.clone(), |_| None), expected);
            let shell = ancestor(43, "/bin/sh");
            assert_eq!(hook_ancestor(provider, shell.clone(), |_| Some(native.clone())), expected);
            assert_eq!(hook_ancestor(provider, ancestor(44, "/bin/bash"), |p| {
                Some(if p.pid == 44 { shell.clone() } else { native.clone() })
            }), expected);
        }
        for path in ["/usr/bin/node", "/other/versions/2.1.273", "/usr/bin/python", "/opt/codex"] {
            assert_eq!(hook_ancestor("claude", ancestor(42, path), |_| Some(ancestor(99, "/opt/claude"))), None);
        }
        assert_eq!(hook_ancestor("unknown", ancestor(42, "/opt/unknown"), |_| None), None);
        assert_eq!(hook_ancestor("codex", ancestor(42, "/bin/sh"), |_| None), None);
        assert_eq!(hook_ancestor("codex", ancestor(42, "/bin/sh"), |_| Some(ancestor(42, "/bin/sh"))), None);
        assert_eq!(hook_ancestor("codex", ancestor(42, "/bin/sh"), |p| Some(ancestor(p.pid + 1, "/bin/sh"))), None);
        let mut missing_birth = ancestor(42, "/opt/codex");
        missing_birth.birth_micros = 0;
        assert_eq!(hook_ancestor("codex", missing_birth, |_| None), None);
    }

    /// `local-session-hook grok session-start` typed in a shell inside a Claude session made that
    /// Claude a Grok session (prod e2e 2026-10-22). The hooked agent must be its own executable.
    #[test]
    fn lifecycle_hooked_agent_is_its_own_executable_never_the_nearest_ancestor() {
        for (provider, path) in [
            ("grok", "/opt/homebrew/lib/node_modules/@xai-official/grok/bin/grok-native"),
            ("grok", "/usr/local/bin/grok"),
            ("cursor", "/Users/a/.local/share/cursor-agent/versions/2026.10.19-6bbf2c4/node"),
            ("cursor", "/Users/a/.local/bin/cursor-agent"),
        ] {
            let native = ancestor(42, path);
            let shell = ancestor(43, "/bin/zsh");
            assert_eq!(hook_ancestor(provider, shell, |_| Some(native.clone())), Some(HookProcess::of(&native)), "{path}");
        }
        for provider in ["grok", "cursor"] {
            for path in ["/Users/a/.local/share/claude/versions/2.1.306", "/opt/claude", "/Applications/Codex.app/Contents/Resources/codex",
                "/usr/bin/node", "/usr/bin/python3", "/Users/a/.local/share/cursor-agent/versions/1/claude"] {
                let shell = ancestor(43, "/bin/zsh");
                assert_eq!(hook_ancestor(provider, shell, |_| Some(ancestor(42, path))), None, "{provider} {path}");
            }
        }
        assert!(!hooked_agent("cursor", std::path::Path::new("/opt/grok")) && !hooked_agent("grok", std::path::Path::new("/x/cursor-agent")));
    }

    #[test]
    fn lifecycle_generation_is_not_taken_from_provider_stdin() {
        let stdin = json!({"session_id":"abc", "hook_event_name":"PermissionRequest",
            "pid": 2, "birth_micros": 1, "sourceProcess": {"pid": 2, "birthMicros": 1}});
        let held = super::super::approval::Held { id: "appr-0123456789abcdef0123456789abcdef".into(),
            input_digest: "d".repeat(64), reply: "/tmp/reply.sock".into(), exact: true, modes: vec![], full: false, similar: None };
        for bytes in [build_datagram("codex", &stdin), build_held_datagram("codex", &stdin, &held),
            build_release_datagram("codex", &stdin, &held.id)] {
            let parsed = parse_datagram(&bytes).unwrap();
            assert_eq!(parsed.source_process, hook_process("codex"));
            assert_ne!(parsed.source_process, Some(HookProcess { pid: 2, birth_micros: 1 }));
        }
        // Process identity is essential even when every optional field is dropped.
        let source = HookProcess { pid: 42, birth_micros: 123456789 };
        let huge = json!({"session_id":"abc", "hook_event_name":"Stop", "transcript_path":"x".repeat(2000),
            "cwd":"y".repeat(2000), "last_assistant_message":"z".repeat(2000)});
        let bytes = build("codex", &huge, Some(&held), Some(source.clone()));
        assert!(bytes.len() <= DATAGRAM_MAX);
        let parsed = parse_datagram(&bytes).unwrap();
        assert_eq!(parsed.source_process, Some(source));
        assert_eq!(parsed.held, Some(held));
    }

    #[test]
    #[ignore = "requires an explicitly isolated native provider invoking this test as its hook"]
    fn lifecycle_owned_native_hook_generation() {
        use std::io::Read;
        let provider = std::env::var("LIGHTBULB_HOOK_PROBE_PROVIDER").unwrap();
        let dir = std::env::var("LIGHTBULB_HOOK_PROBE_DIR").unwrap();
        let mut bytes = Vec::new();
        std::io::stdin().take(65536).read_to_end(&mut bytes).unwrap();
        let stdin: Value = serde_json::from_slice(&bytes).unwrap();
        let source = hook_process(&provider).expect("actual native provider ancestor");
        let native = super::super::process(source.pid).unwrap();
        let held = super::super::approval::Held { id: "appr-0123456789abcdef0123456789abcdef".into(),
            input_digest: "d".repeat(64), reply: "/tmp/reply.sock".into(), exact: true, modes: vec![], full: false, similar: None };
        for bytes in [build_datagram(&provider, &stdin), build_held_datagram(&provider, &stdin, &held),
            build_release_datagram(&provider, &stdin, &held.id)] {
            assert_eq!(parse_datagram(&bytes).unwrap().source_process, Some(source.clone()));
        }
        let proof = json!({"provider":provider,"event":stdin["hook_event_name"],
            "sourceProcess":source,"executable":native.executable,"hookPid":std::process::id()});
        let path = std::path::Path::new(&dir).join(format!("{}.json", uuid::Uuid::new_v4()));
        std::fs::write(path, serde_json::to_vec(&proof).unwrap()).unwrap();
    }

    #[test]
    fn lifecycle_v1_cannot_upgrade_to_an_attributed_hook() {
        for event in ["Stop", "SessionEnd", "SessionStart", "PermissionRequest", "PermissionRelease"] {
            let wire = json!({"v":1,"provider":"codex","event":event,"sessionId":"abc",
                "sourceProcess":{"pid":42,"birthMicros":123},
                "approvalId":"appr-0123456789abcdef0123456789abcdef"});
            assert!(parse_datagram(&serde_json::to_vec(&wire).unwrap()).is_none(), "{event}");
        }
        // Absent/null identity keeps the exact-ID legacy release protocol.
        let mut release = json!({"v":1,"provider":"codex","event":"PermissionRelease","sessionId":"abc",
            "approvalId":"appr-0123456789abcdef0123456789abcdef"});
        for source in [None, Some(Value::Null)] {
            if let Some(source) = source { release["sourceProcess"] = source; }
            let parsed = parse_datagram(&serde_json::to_vec(&release).unwrap()).unwrap();
            assert_eq!(parsed.source_process, None);
            assert_eq!(transition(&parsed), Transition::Release("appr-0123456789abcdef0123456789abcdef".into()));
        }
    }

    #[test]
    fn lifecycle_generation_parsing_fails_closed() {
        let legacy = json!({"v":1,"provider":"codex","event":"Stop","sessionId":"abc"});
        assert_eq!(parse_datagram(&serde_json::to_vec(&legacy).unwrap()).unwrap().source_process, None);
        for source in [json!({"pid":42}), json!({"pid":0,"birthMicros":123}),
            json!({"pid":42,"birthMicros":0}), json!({"pid":4294967295u32,"birthMicros":123}),
            json!({"pid":"42","birthMicros":123})] {
            let mut wire = legacy.clone(); wire["v"] = json!(2); wire["sourceProcess"] = source;
            assert!(parse_datagram(&serde_json::to_vec(&wire).unwrap()).is_none());
        }
        let mut unknown = legacy;
        unknown["v"] = json!(3);
        assert!(parse_datagram(&serde_json::to_vec(&unknown).unwrap()).is_none());
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
        // `/exit` and `/logout` are the person's own end; a hang-up or a kill ("other") is not.
        for (reason, ended) in [("prompt_input_exit", ON_PURPOSE), ("logout", ON_PURPOSE), ("other", "exited"), ("clear", "exited")] {
            assert_eq!(transition(&HookEvent { reason: Some(reason.into()), ..ev("SessionEnd") }), Transition::Ended(ended), "{reason}");
        }
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
            "tool_input":{"requestId":"e12fd3e8-32ca-4b65-8eaf-ab979e3c9449"}});
        let ordinary = parse_datagram(&build_datagram("claude", &stdin)).unwrap();
        assert_eq!(ordinary.waiting_text.as_deref(), Some("Lightbulb message"));
        let held = super::super::approval::Held {
            id: "appr-0123456789abcdef0123456789abcdef".into(),
            input_digest: "d".repeat(64), reply: "/tmp/appr.sock".into(),
            exact: true, modes: vec![], full: false, similar: None,
        };
        let prompt = parse_datagram(&build_held_datagram("claude", &stdin, &held)).unwrap();
        assert_eq!(prompt.waiting_text.as_deref(), Some("Lightbulb message"));

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
