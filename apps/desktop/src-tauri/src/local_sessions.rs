//! Opt-in native terminal sharing. Provider processes and credentials stay local.
mod approval;
mod claude;
mod codex;
pub mod enrollment;
pub(crate) mod lifecycle;
pub(crate) mod snapshot;
mod stats;
mod transport;
mod workspace;
pub use claude::{
    supported_version as supported_claude_version,
    verify_current_generation as verify_claude_runtime,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::MetadataExt},
    },
    path::{Path, PathBuf},
};

pub type Result<T> = std::result::Result<T, String>;

/// Shared with the retained, rerunnable native probe so privacy filtering has
/// one implementation. Only version-bound completed public text is supported.
pub fn normalize_public_message(provider: &str, session: &str, row: &Value) -> Result<Vec<Value>> {
    match provider {
        "claude" => claude::normalize(session, row),
        "codex" => codex::normalize(session, row),
        _ => Err("unsupported provider".into()),
    }
}

pub(crate) const MAX_TEXT: usize = 12 * 1024;
pub(crate) const MAX_TOOL_INPUT: usize = 4 * 1024;
pub(crate) const MAX_TOOL_RESULT: usize = 12 * 1024;
pub(crate) const PREVIEW_LINES: usize = 20;
// One native JSONL row may contain duplicated command output or private compaction data.
pub const MAX_JSONL_ROW: usize = 4 * 1024 * 1024;
pub(crate) const HEADER_PREFIX: &str = "[Lightbulb teammate contribution";
/// The owner's own prompt from the Lightbulb app: plain provenance, no teammate rules.
pub(crate) const OWNER_LABEL: &str = "[Sent from Lightbulb by you]\n";
pub(crate) const NOTICE_PREFIX: &str = "[Lightbulb workspace advisory: ";

/// Cut `text` to at most `max` bytes on a char boundary.
pub(crate) fn bounded(text: &str, max: usize) -> (String, bool) {
    if text.len() <= max {
        return (text.to_owned(), false);
    }
    let mut end = max;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    (text[..end].to_owned(), true)
}

/// The first `lines` lines plus the total line count, as the TUI's collapsed output does.
pub(crate) fn preview(text: &str, lines: usize) -> (String, u64) {
    (
        text.lines().take(lines).collect::<Vec<_>>().join("\n"),
        text.lines().count() as u64,
    )
}

fn looks_secret(word: &str) -> bool {
    let tail_ok = |s: &str| {
        s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
    };
    (word.starts_with("AKIA")
        && word.len() == 20
        && word
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()))
        || (word.starts_with("sk-") && word.len() >= 24 && tail_ok(&word[3..]))
        || ((word.starts_with("ghp_")
            || word.starts_with("gho_")
            || word.starts_with("github_pat_"))
            && word.len() >= 30)
        || ((word.starts_with("xoxb-") || word.starts_with("xoxp-") || word.starts_with("xoxa-"))
            && word.len() >= 20)
}

fn redact_pem(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("-----BEGIN ") {
        out.push_str(&rest[..start]);
        out.push_str("[redacted]");
        // A 20-line tool preview can cut a key between BEGIN and END. Fail closed:
        // drop everything from the header on rather than ship the key body.
        let Some(end) = rest[start..].find("-----END ") else {
            return out;
        };
        let tail = &rest[start + end + 9..];
        let close = tail.find("-----").map(|i| i + 5).unwrap_or(tail.len());
        rest = &tail[close..];
    }
    out.push_str(rest);
    out
}

/// Credential-shaped tokens become "[redacted]". A backstop, never a guarantee.
pub(crate) fn redact(text: &str) -> String {
    fn flush(word: &mut String, previous: &mut String, out: &mut String) {
        if word.is_empty() {
            return;
        }
        let bearer = previous.eq_ignore_ascii_case("bearer") && word.len() >= 20;
        out.push_str(if bearer || looks_secret(word) {
            "[redacted]"
        } else {
            word
        });
        *previous = std::mem::take(word);
    }
    let text = redact_pem(text);
    let mut out = String::with_capacity(text.len());
    let (mut word, mut previous) = (String::new(), String::new());
    for c in text.chars() {
        if c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '+' | '/' | '.') {
            word.push(c);
        } else {
            flush(&mut word, &mut previous, &mut out);
            out.push(c);
        }
    }
    flush(&mut word, &mut previous, &mut out);
    out
}

/// Redact every string leaf, then shrink string leaves until the JSON fits `max` bytes; Null if it
/// cannot. The flag is true when anything was cut or nulled, so the caller can mark its event.
pub(crate) fn bound_json(mut value: Value, max: usize) -> (Value, bool) {
    fn walk(v: &mut Value, f: &dyn Fn(&mut String)) {
        match v {
            Value::String(s) => f(s),
            Value::Array(a) => a.iter_mut().for_each(|x| walk(x, f)),
            Value::Object(o) => o.values_mut().for_each(|x| walk(x, f)),
            _ => {}
        }
    }
    walk(&mut value, &|s| *s = redact(s));
    let mut per = max;
    let mut shrunk = false;
    while serde_json::to_string(&value)
        .map(|s| s.len() > max)
        .unwrap_or(true)
    {
        if per < 64 {
            return (Value::Null, true);
        }
        per /= 2;
        shrunk = true;
        walk(&mut value, &|s| {
            let (cut, truncated) = bounded(s, per);
            if truncated {
                *s = format!("{cut}…");
            }
        });
    }
    (value, shrunk)
}

pub(crate) fn event(
    kind: &str,
    role: &str,
    text: &str,
    ts: Option<&str>,
    item_id: Option<&str>,
) -> Value {
    let (text, truncated) = bounded(&redact(text), MAX_TEXT);
    let mut value = json!({"kind": kind, "role": role, "text": text, "truncated": truncated});
    if let Some(ts) = ts {
        value["ts"] = json!(ts);
    }
    if let Some(id) = item_id {
        value["itemId"] = json!(id);
    }
    value
}

pub(crate) fn lifecycle(name: &str, text: &str, ts: Option<&str>, item_id: Option<&str>) -> Value {
    let mut value = event("lifecycle", "system", text, ts, item_id);
    value["lifecycle"] = json!(name);
    value
}

/// Split a Lightbulb-authored ingress text into (provenance, teammate text). None for ordinary native input.
pub(crate) fn teammate_contribution(text: &str) -> Option<(Value, String)> {
    let rest = text.strip_prefix(HEADER_PREFIX)?;
    let header_end = rest.find("}]\n")?;
    let header = &rest[..header_end + 1];
    let provenance: Value = serde_json::from_str(&header[header.find("{\"")?..]).ok()?;
    if !provenance.is_object() {
        return None;
    }
    let mut body = &rest[header_end + 3..];
    if body.starts_with(NOTICE_PREFIX) {
        if let Some(end) = body.find("]\n") {
            body = &body[end + 2..];
        }
    }
    Some((provenance, body.to_owned()))
}

pub(crate) fn actor_of(provenance: &Value) -> Option<Value> {
    if let Some(user) = provenance["actorUserId"].as_str() {
        return Some(json!({"userId": user}));
    }
    provenance["actor"]["id"]
        .as_str()
        .map(|id| json!({"agentId": id}))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Enrollment {
    pub owner_id: String,
    pub human_user_id: String,
    pub device_id: String,
    pub gateway_url: String,
    pub enabled: bool,
    pub paused: bool,
    #[serde(default)]
    pub server_revision: Option<String>,
    #[serde(default)]
    pub remote_disabled: bool,
    #[serde(default)]
    pub attempt: Option<EnrollmentAttempt>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EnrollmentAttempt {
    pub previous_revision: Option<String>,
}

impl Enrollment {
    fn authenticate(&mut self, human: Option<Option<&str>>) {
        // Outer `None` means the webview hasn't reported a signed-in user yet
        // (unknown), not that the user signed out -- leave `paused` as is.
        if let Some(human) = human {
            self.paused = human != Some(self.human_user_id.as_str());
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct AgentActor {
    kind: String,
    id: String,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ActorLabel {
    pub kind: String,
    pub handle: Option<String>,
    pub display_name: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Command {
    request_id: String,
    expected_generation: String,
    kind: String,
    text: Option<String>,
    expected_turn_id: Option<String>,
    session_id: String,
    room_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor_user_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    actor: Option<AgentActor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    invocation_expires_at: Option<u64>,
    command_seq: u64,
    state: String,
    created_at: u64,
    deliver_by: u64,
    held: bool,
    provider_message_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    approval_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    decision: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    mode: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    input_digest: Option<String>,
    #[serde(skip)]
    pub(crate) label: Option<ActorLabel>,
    /// The sender is this Mac's own enrolled human (server-supplied `actorUserId`).
    #[serde(skip)]
    pub(crate) own: bool,
}

impl Command {
    fn authorized_at(&self, now: u64) -> bool {
        now < self.deliver_by
            && match (&self.actor_user_id, &self.actor, self.invocation_expires_at) {
                (Some(human), None, None) => !human.is_empty(),
                (None, Some(actor), Some(expiry)) => {
                    actor.kind == "agent" && !actor.id.is_empty() && now < expiry
                }
                _ => false,
            }
    }
    fn provenance(&self) -> Value {
        let mut value = json!({"roomId":self.room_id,"requestId":self.request_id});
        if let Some(actor) = &self.actor {
            value["actor"] = json!(actor);
        } else {
            value["actorUserId"] = json!(self.actor_user_id);
        }
        if let Some(label) = &self.label {
            if let Some(handle) = &label.handle {
                value["actorHandle"] = json!(handle);
            }
            if let Some(name) = &label.display_name {
                value["actorDisplayName"] = json!(name);
            }
        }
        value
    }
    /// Byte-exact header both adapters strip again on the way back (`teammate_contribution`).
    /// Labels are display text: braces, brackets and newlines are dropped so they can never
    /// be mistaken for the header's own delimiters or the provenance JSON.
    pub(crate) fn header(&self) -> String {
        if self.own {
            return OWNER_LABEL.into();
        }
        let clean = |s: &str| -> String {
            s.chars()
                .filter(|c| !matches!(c, '{' | '}' | '[' | ']' | '\n' | '\r'))
                .collect()
        };
        let who = match &self.label {
            Some(ActorLabel {
                handle: Some(handle),
                display_name,
                ..
            }) => match display_name {
                Some(name) => format!(" from @{} ({})", clean(handle), clean(name)),
                None => format!(" from @{}", clean(handle)),
            },
            _ => String::new(),
        };
        format!("{HEADER_PREFIX}{who} {}]\n", self.provenance())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Receipt {
    hash: Option<String>,
    generation: String,
    outcome: String,
    provider_message_id: Option<String>,
    held: bool,
    #[serde(default)]
    ingress: Option<Ingress>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Ingress {
    native: NativeSession,
    cursor: Cursor,
    text: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    workspace_revision: Option<u64>,
}

struct Journal {
    dir: PathBuf,
}
impl Journal {
    fn path(&self, session: &str, request: &str) -> PathBuf {
        self.dir.join(format!(
            "{}.json",
            fingerprint(&json!([session, request]).to_string())
        ))
    }

    fn admit(&self, cmd: &Command, generation: &str, now: u64) -> Result<bool> {
        let path = self.path(&cmd.session_id, &cmd.request_id);
        let hash = fingerprint(&serde_json::to_string(cmd).map_err(|e| e.to_string())?);
        if path.try_exists().map_err(|e| e.to_string())? {
            let receipt: Receipt = private_read(&path)?;
            if receipt.hash.as_ref().is_some_and(|old| old != &hash) {
                return Err("command identity conflict".into());
            }
            return Ok(false);
        }
        let order_path = self
            .dir
            .join(format!("order-{}.json", fingerprint(&cmd.session_id)));
        let ordered = if order_path.try_exists().map_err(|e| e.to_string())? {
            let last: (u64, String) = private_read(&order_path)?;
            let receipt: Receipt = private_read(&self.path(&cmd.session_id, &last.1))?;
            cmd.command_seq > last.0
                && (receipt.outcome != "unknown" || receipt.generation != generation)
        } else {
            true
        };
        let shaped = match cmd.kind.as_str() {
            "send" => cmd
                .text
                .as_ref()
                .is_some_and(|t| !t.trim().is_empty() && t.len() <= 8192),
            "approve" => {
                cmd.approval_id.is_some()
                    && cmd.input_digest.is_some()
                    && matches!(cmd.decision.as_deref(), Some("allow" | "deny"))
                    && cmd.text.as_ref().is_none_or(|t| t.len() <= 500)
            }
            _ => false,
        };
        let admit = ordered
            && cmd.expected_generation == generation
            && cmd.authorized_at(now)
            && cmd.created_at <= now
            && cmd.deliver_by.saturating_sub(cmd.created_at) <= 30
            && cmd.state == "dispatched"
            && shaped
            && cmd.expected_turn_id.is_none()
            && cmd.command_seq > 0;
        // Order first: a crash between these writes must block the next send,
        // not leave an unknown native intent invisible to sequence fencing.
        if admit {
            private_write(&order_path, &(cmd.command_seq, cmd.request_id.clone()))?;
        }
        private_write(
            &path,
            &Receipt {
                hash: Some(hash),
                generation: cmd.expected_generation.clone(),
                outcome: if admit { "unknown" } else { "rejected" }.into(),
                provider_message_id: None,
                held: false,
                ingress: None,
            },
        )?;
        // Intent is durable before returning permission to perform the native side effect.
        Ok(admit)
    }
    fn reconcile(&self, session: &str, request: &str, generation: &str) -> Result<Receipt> {
        let path = self.path(session, request);
        if path.try_exists().map_err(|e| e.to_string())? {
            let receipt: Receipt = private_read(&path)?;
            if receipt.generation != generation {
                return Err("receipt generation conflict".into());
            }
            return Ok(receipt);
        }
        // The supervisor serializes reconciliation and admission. Persisting this
        // tombstone fences even a command frame that arrives after reconciliation.
        let receipt = Receipt {
            hash: None,
            generation: generation.into(),
            outcome: "rejected".into(),
            provider_message_id: None,
            held: false,
            ingress: None,
        };
        private_write(&path, &receipt)?;
        Ok(receipt)
    }
    fn anchor(
        &self,
        command: &Command,
        native: &NativeSession,
        text: String,
        workspace_revision: Option<u64>,
    ) -> Result<()> {
        let path = self.path(&command.session_id, &command.request_id);
        let mut receipt: Receipt = private_read(&path)?;
        if receipt.outcome != "unknown" || receipt.ingress.is_some() {
            return Err("intent already anchored".into());
        }
        let mut file = secure_file(native.transcript.as_ref().ok_or("transcript unavailable")?)?;
        let meta = file.metadata().map_err(|_| "transcript unavailable")?;
        if meta.len() > 0 {
            file.seek(SeekFrom::End(-1))
                .map_err(|_| "transcript unavailable")?;
            let mut last = [0];
            file.read_exact(&mut last)
                .map_err(|_| "transcript unavailable")?;
            if last[0] != b'\n' {
                return Err("partial transcript boundary".into());
            }
        }
        receipt.ingress = Some(Ingress {
            workspace_revision,
            native: native.clone(),
            cursor: Cursor {
                offset: meta.len(),
                inode: Some(meta.ino()),
            },
            text,
        });
        private_write(&path, &receipt)
    }
    fn finish(
        &self,
        session: &str,
        request: &str,
        id: Option<String>,
        held: bool,
    ) -> Result<Receipt> {
        let path = self.path(session, request);
        let mut receipt: Receipt = private_read(&path)?;
        if receipt.outcome == "unknown" {
            receipt.outcome = if id.is_some() { "applied" } else { "rejected" }.into();
            receipt.provider_message_id = id;
            receipt.held = held;
            private_write(&path, &receipt)?;
        }
        Ok(receipt)
    }
}

#[derive(Default, Clone, Debug, Serialize, Deserialize)]
struct Cursor {
    offset: u64,
    inode: Option<u64>,
}
impl Cursor {
    fn read(&mut self, path: &Path) -> Result<Vec<Value>> {
        let mut file = secure_file(path)?;
        let meta = file.metadata().map_err(|e| e.to_string())?;
        if self.inode.is_some_and(|i| i != meta.ino()) || meta.len() < self.offset {
            return Err("history-gap".into());
        }
        self.inode = Some(meta.ino());
        file.seek(SeekFrom::Start(self.offset))
            .map_err(|e| e.to_string())?;
        let mut reader = BufReader::new(file.take((MAX_JSONL_ROW + 1) as u64));
        let mut rows = vec![];
        let mut consumed = 0;
        while consumed < 65536 && rows.len() < 32 {
            let mut bytes = vec![];
            let n = reader
                .read_until(b'\n', &mut bytes)
                .map_err(|e| e.to_string())?;
            if n > MAX_JSONL_ROW {
                return Err("history-gap".into());
            }
            if !bytes.ends_with(b"\n") {
                break;
            }
            rows.push(serde_json::from_slice(&bytes).map_err(|_| "history-gap")?);
            self.offset += n as u64;
            consumed += n;
        }
        Ok(rows)
    }
}

fn private_write(path: &Path, value: &impl Serialize) -> Result<()> {
    if fs::symlink_metadata(path)
        .is_ok_and(|m| !m.is_file() || m.uid() != unsafe { libc::geteuid() })
    {
        return Err("unsafe local state path".into());
    }
    let parent = path.parent().ok_or("missing state directory")?;
    let directory = open_directory(parent)?;
    let temp = std::ffi::CString::new(format!(".{}.tmp", uuid::Uuid::new_v4())).unwrap();
    let name = std::ffi::CString::new(path.file_name().ok_or("missing state filename")?.as_bytes())
        .map_err(|_| "invalid state filename")?;
    let result = (|| {
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                temp.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC | libc::O_NOFOLLOW,
                0o600,
            )
        };
        if fd < 0 {
            return Err("cannot create private state".into());
        }
        let mut file = unsafe { fs::File::from_raw_fd(fd) };
        file.write_all(&serde_json::to_vec(value).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())?;
        file.sync_all().map_err(|e| e.to_string())?;
        if unsafe {
            libc::renameat(
                directory.as_raw_fd(),
                temp.as_ptr(),
                directory.as_raw_fd(),
                name.as_ptr(),
            )
        } != 0
        {
            return Err("cannot replace private state".into());
        }
        directory.sync_all().map_err(|e| e.to_string())
    })();
    if result.is_err() {
        unsafe { libc::unlinkat(directory.as_raw_fd(), temp.as_ptr(), 0) };
    }
    result
}

fn open_directory(path: &Path) -> Result<fs::File> {
    // macOS system aliases are fixed; every user-controlled component is opened
    // relative to a held descriptor with O_NOFOLLOW, including parent directories.
    let normalized = if let Ok(rest) = path.strip_prefix("/tmp") {
        Path::new("/private/tmp").join(rest)
    } else if let Ok(rest) = path.strip_prefix("/var") {
        Path::new("/private/var").join(rest)
    } else {
        path.to_path_buf()
    };
    if !normalized.is_absolute() {
        return Err("absolute local path required".into());
    }
    let mut dir = fs::File::open("/").map_err(|e| e.to_string())?;
    for part in normalized.components() {
        if matches!(part, std::path::Component::RootDir) {
            continue;
        }
        let std::path::Component::Normal(name) = part else {
            return Err("unsafe local path component".into());
        };
        let name = std::ffi::CString::new(name.as_bytes()).map_err(|_| "invalid local path")?;
        let fd = unsafe {
            libc::openat(
                dir.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
            )
        };
        if fd < 0 {
            return Err("unsafe or unavailable local directory".into());
        }
        dir = unsafe { fs::File::from_raw_fd(fd) };
    }
    Ok(dir)
}

fn secure_file(path: &Path) -> Result<fs::File> {
    let dir = open_directory(path.parent().ok_or("missing local directory")?)?;
    let name = std::ffi::CString::new(path.file_name().ok_or("missing local filename")?.as_bytes())
        .map_err(|_| "invalid local path")?;
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            name.as_ptr(),
            // A replaced FIFO must not block before fstat rejects the descriptor.
            // O_NONBLOCK does not change regular-file reads used by every caller.
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
        )
    };
    if fd < 0 {
        return Err("unsafe or unavailable local file".into());
    }
    let file = unsafe { fs::File::from_raw_fd(fd) };
    let meta = file.metadata().map_err(|e| e.to_string())?;
    if !meta.is_file() || meta.uid() != unsafe { libc::geteuid() } {
        return Err("unsafe local file".into());
    }
    Ok(file)
}

fn private_read<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let file = secure_file(path)?;
    if file.metadata().map_err(|e| e.to_string())?.mode() & 0o077 != 0 {
        return Err("local state is not private".into());
    }
    serde_json::from_reader(file.take(16 * 1024 * 1024)).map_err(|e| e.to_string())
}

fn fingerprint(value: &str) -> String {
    hex::encode(Sha256::digest(value.as_bytes()))
}

fn private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::DirBuilderExt;
    if !path.try_exists().map_err(|e| e.to_string())? {
        fs::DirBuilder::new()
            .mode(0o700)
            .create(path)
            .map_err(|e| e.to_string())?;
    }
    let meta = fs::symlink_metadata(path).map_err(|e| e.to_string())?;
    if !meta.is_dir() || meta.uid() != unsafe { libc::geteuid() } || meta.mode() & 0o077 != 0 {
        return Err("unsafe local sharing directory".into());
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Process {
    pid: u32,
    birth_micros: u64,
    executable: PathBuf,
}

/// A process is its pid and start time; `executable` is only the name it was read
/// under. macOS reports a hard-linked binary by whichever name was looked up last
/// (Claude's `versions/<v>` and `ClaudeCode.app/Contents/MacOS/claude`), so comparing
/// paths ended live sessions and minted new generations for unchanged processes.
impl PartialEq for Process {
    fn eq(&self, other: &Self) -> bool {
        self.pid == other.pid && self.birth_micros == other.birth_micros
    }
}

impl Eq for Process {}

impl Process {
    fn generation(&self) -> String {
        fingerprint(&format!("{}:{}", self.pid, self.birth_micros))
    }
}

fn process(pid: u32) -> Result<Process> {
    #[cfg(target_os = "macos")]
    {
        if pid == 0 || pid > i32::MAX as u32 {
            return Err("invalid process".into());
        }
        let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
        let size = std::mem::size_of_val(&info) as i32;
        if unsafe {
            libc::proc_pidinfo(
                pid as i32,
                libc::PROC_PIDTBSDINFO,
                0,
                &mut info as *mut _ as *mut _,
                size,
            )
        } != size
            || info.pbi_pid != pid
            || info.pbi_uid != unsafe { libc::geteuid() }
            || info.pbi_ruid != unsafe { libc::getuid() }
        {
            return Err("process unavailable".into());
        }
        let mut buffer = [0u8; 4096];
        let n = unsafe {
            libc::proc_pidpath(
                pid as i32,
                buffer.as_mut_ptr() as *mut _,
                buffer.len() as u32,
            )
        };
        if n <= 0 {
            return Err("process executable unavailable".into());
        }
        let executable = std::str::from_utf8(&buffer[..n as usize])
            .map_err(|_| "invalid executable path")?
            .trim_end_matches('\0');
        Ok(Process {
            pid,
            birth_micros: info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec,
            executable: executable.into(),
        })
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = pid;
        Err("local sharing supports macOS only".into())
    }
}

fn confined_path(root: &Path, path: &Path) -> Result<PathBuf> {
    let relative = path
        .strip_prefix(root)
        .map_err(|_| "unsupported profile root")?;
    let mut current = root.to_path_buf();
    if fs::symlink_metadata(root)
        .map_err(|_| "profile root unavailable")?
        .file_type()
        .is_symlink()
    {
        return Err("symlink profile root".into());
    }
    for component in relative.components() {
        if !matches!(component, std::path::Component::Normal(_)) {
            return Err("profile escape".into());
        }
        current.push(component);
        let meta = fs::symlink_metadata(&current).map_err(|_| "profile file unavailable")?;
        if meta.file_type().is_symlink() || meta.uid() != unsafe { libc::geteuid() } {
            return Err("unsafe profile file".into());
        }
    }
    Ok(path.to_path_buf())
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct NativeSession {
    provider: String,
    conversation: String,
    profile: String,
    process: Process,
    transcript: Option<PathBuf>,
    socket: Option<PathBuf>,
    socket_inode: Option<u64>,
    version: String,
    restriction: Option<String>,
    status: String,
    #[serde(default)]
    title: Option<String>,
    #[serde(default)]
    cwd_name: Option<String>,
    #[serde(default)]
    active_turn: Option<String>,
    #[serde(default)]
    pub last_assistant_message: Option<String>,
    /// A Claude Code background job. Mirrored like a terminal session; the relay keeps
    /// it out of human listings. Mirrors persisted before it existed read as interactive.
    #[serde(default)]
    background: bool,
}

async fn native_output(program: &Path, args: &[&str]) -> Result<String> {
    let output = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        tokio::process::Command::new(program)
            .args(args)
            .env_remove("CODEX_HOME")
            .env_remove("CLAUDE_CONFIG_DIR")
            .stdin(std::process::Stdio::null())
            .kill_on_drop(true)
            .output(),
    )
    .await
    .map_err(|_| "native metadata request timed out")?
    .map_err(|_| "native executable unavailable")?;
    if !output.status.success() || output.stdout.len() > 1024 * 1024 {
        return Err("native request failed".into());
    }
    String::from_utf8(output.stdout).map_err(|_| "invalid native metadata".into())
}

pub(crate) fn provider_program(home: &Path, name: &str) -> Option<PathBuf> {
    let mut directories = vec![
        home.join(".local/bin"),
        PathBuf::from("/opt/homebrew/bin"),
        PathBuf::from("/usr/local/bin"),
    ];
    if let Some(value) = std::env::var_os("PATH") {
        directories.extend(std::env::split_paths(&value));
    }
    let program = directories.into_iter().find_map(|dir| {
        fs::canonicalize(dir.join(name))
            .ok()
            .filter(|p| p.is_file())
    })?;
    if name == "codex" && program.file_name().is_some_and(|n| n == "codex.js") {
        // The official npm launcher starts this packaged native executable.
        // Inspect and invoke the mapped binary itself for runtime verification.
        let arch = if cfg!(target_arch = "aarch64") {
            "arm64"
        } else {
            "x64"
        };
        let triple = if cfg!(target_arch = "aarch64") {
            "aarch64"
        } else {
            "x86_64"
        };
        return fs::canonicalize(program.parent()?.parent()?.join(format!(
            "node_modules/@openai/codex-darwin-{arch}/vendor/{triple}-apple-darwin/bin/codex"
        )))
        .ok();
    }
    Some(program)
}

async fn open_files(pid: u32) -> Result<Vec<PathBuf>> {
    Ok(native_output(
        Path::new("/usr/sbin/lsof"),
        &["-n", "-a", "-p", &pid.to_string(), "-Fn"],
    )
    .await?
    .lines()
    .filter_map(|l| l.strip_prefix('n'))
    .filter(|p| p.starts_with('/'))
    .map(PathBuf::from)
    .collect())
}

/// One install can reach the same binary under two names: Claude's installer
/// hard-links `<root>/ClaudeCode.app/Contents/MacOS/claude` onto the
/// `<root>/versions/<v>` file the PATH launcher resolves to, and sessions started
/// after that run under the bundle path. Accept a second name only when it is
/// literally the same file -- same device, same inode -- reached without a symlink
/// from inside the install root. Nothing here is ever executed: `installed`, the
/// PATH-resolved binary, stays the only path this module runs.
fn same_installed_file(executable: &Path, installed: &Path) -> Result<()> {
    if executable == installed {
        return Ok(());
    }
    let root = installed
        .parent()
        .and_then(Path::parent)
        .ok_or("runtime-unverified")?;
    confined_path(root, executable).map_err(|_| "runtime-unverified")?;
    let running = fs::symlink_metadata(executable).map_err(|_| "runtime-unverified")?;
    let known = fs::metadata(installed).map_err(|_| "runtime-unverified")?;
    if !running.is_file() || running.dev() != known.dev() || running.ino() != known.ino() {
        return Err("runtime-unverified".into());
    }
    Ok(())
}

async fn runtime_version(expected: &Process, installed: &Path, provider: &str) -> Result<String> {
    // Never execute an arbitrary path obtained from a registration or process row.
    // The mapped executable must be another name for the installed provider binary.
    same_installed_file(&expected.executable, installed)?;
    if process(expected.pid)? != *expected {
        return Err("runtime-unverified".into());
    }
    let before = fs::metadata(installed).map_err(|_| "runtime-unverified")?;
    let mapped = native_output(
        Path::new("/usr/sbin/lsof"),
        &[
            "-n",
            "-a",
            "-p",
            &expected.pid.to_string(),
            "-d",
            "txt",
            "-Fin",
        ],
    )
    .await?;
    let mut inode = None;
    let mut matched = false;
    for line in mapped.lines() {
        if let Some(value) = line.strip_prefix('i') {
            inode = value.parse::<u64>().ok();
        }
        if let Some(value) = line.strip_prefix('n') {
            // The inode is the identity; lsof may report the install's other name.
            if (Path::new(value) == expected.executable || Path::new(value) == installed)
                && inode == Some(before.ino())
            {
                matched = true;
            }
        }
    }
    if !matched {
        return Err("runtime-unverified".into());
    }
    let output = native_output(installed, &["--version"]).await?;
    let version = output
        .split_whitespace()
        .nth(if provider == "codex" { 1 } else { 0 })
        .ok_or("runtime-unverified")?;
    let after = fs::metadata(installed).map_err(|_| "runtime-unverified")?;
    if before.ino() != after.ino()
        || before.mtime() != after.mtime()
        || before.mtime_nsec() != after.mtime_nsec()
        || process(expected.pid)? != *expected
    {
        return Err("runtime-unverified".into());
    }
    Ok(version.into())
}

/// Merges one listing into what is already known. A session the listing does not
/// mention keeps everything it had: a listing is a sweep, not an obituary, and a
/// session ends only when its hook or its pid says so.
fn reconcile_sessions(current: &mut Vec<NativeSession>, discovered: Vec<NativeSession>) {
    let mut seen = std::collections::BTreeMap::new();
    for mut session in discovered {
        let identity = (
            session.provider.clone(),
            session.profile.clone(),
            session.conversation.clone(),
        );
        let entry = seen
            .entry(identity)
            .or_insert((session.process.clone(), false));
        entry.1 |= entry.0 != session.process;
        if entry.1 {
            session.restriction = Some("runtime-unverified".into());
        }
        if let Some(existing) = current.iter_mut().find(|s| {
            s.provider == session.provider
                && s.profile == session.profile
                && s.conversation == session.conversation
        }) {
            *existing = session;
        } else {
            current.push(session);
        }
    }
}

fn deadline_now(
    server: u64,
    round_trip: f64,
    elapsed: f64,
    wall_elapsed: Option<f64>,
) -> Option<u64> {
    let wall = wall_elapsed?;
    if !round_trip.is_finite()
        || !elapsed.is_finite()
        || !wall.is_finite()
        || wall < 0.0
        || round_trip < 0.0
        || round_trip > 10.0
        || !(0.0..30.0).contains(&elapsed)
        || (wall - elapsed).abs() > 2.0
    {
        return None;
    }
    // +1 accounts for the server's integer-second timestamp quantization.
    server.checked_add((round_trip + elapsed.max(wall)).ceil() as u64 + 1)
}

fn split_text(event: &Value) -> Vec<Value> {
    let text = event["text"].as_str().unwrap_or("");
    // Lifecycle markers and thinking markers carry no text; they still travel.
    if text.is_empty() {
        return vec![event.clone()];
    }
    // A tool event carries its bounded `tool.result`; chunking would clone it per part
    // and the web keys tool rows by itemId, so the preview text is bounded instead.
    if event["kind"] == "tool" {
        let (cut, truncated) = bounded(text, 8192);
        let mut single = event.clone();
        single["text"] = json!(cut);
        if truncated {
            single["truncated"] = json!(true);
        }
        // Those are UTF-8 byte bounds; the envelope charges 6 wire chars per UTF-16 unit
        // of a non-ASCII char, so a CJK result can still build an event that never fits
        // alone. Shrink the assembled event against the wire itself.
        let mut budget = 8192;
        while budget >= 64 && ascii_wire_len(&single).is_ok_and(|wire| wire > 15_000) {
            budget /= 2;
            single["text"] = json!(bounded(single["text"].as_str().unwrap_or(""), budget).0);
            if let Some(result) = single.pointer_mut("/tool/result") {
                if !result.is_null() {
                    *result = bound_json(result.take(), budget).0;
                }
            }
            single["truncated"] = json!(true);
        }
        return vec![single];
    }
    let mut result = vec![];
    let mut start = 0;
    while start < text.len() {
        let mut end = start;
        let mut wire = 0usize;
        for (index, c) in text[start..].char_indices() {
            let cost = if c.is_ascii() {
                if matches!(c, '"' | '\\') || (c as u32) < 0x20 {
                    6
                } else {
                    1
                }
            } else {
                c.len_utf16() * 6
            };
            if wire + cost > 11_000 && index > 0 {
                break;
            }
            wire += cost;
            end = start + index + c.len_utf8();
        }
        let mut part = event.clone();
        part["text"] = json!(&text[start..end]);
        result.push(part);
        start = end;
    }
    if result.is_empty() {
        result.push(event.clone());
    }
    result
}

/// Make an event acceptable to the backend's contract before it is queued. Harness ids
/// travel verbatim and are not ours to trust; a rejected batch tears the connection down
/// and the same head of queue replays forever.
fn admissible(event: &mut Value) {
    fn opaque(value: &Value) -> bool {
        value.as_str().is_some_and(|s| {
            (1..=256).contains(&s.len())
                && s.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
        })
    }
    if let Some(map) = event.as_object_mut() {
        for key in ["itemId", "parentId", "subagent"] {
            if map.get(key).is_some_and(|v| !opaque(v)) {
                map.remove(key);
            }
        }
    }
    if let Some(ts) = event
        .get("ts")
        .and_then(Value::as_str)
        .map(|t| bounded(t, 40).0)
    {
        event["ts"] = json!(ts);
    }
    let name = event
        .pointer("/tool/name")
        .and_then(Value::as_str)
        .map(|n| bounded(n, 128).0);
    if let Some(name) = name {
        event["tool"]["name"] = json!(if name.is_empty() {
            "tool"
        } else {
            name.as_str()
        });
    }
}

/// Stands in for an event that cannot be sent at all, so the queue keeps moving.
fn unsendable(original: &Value) -> Value {
    let mut marker = event(
        "text",
        "system",
        "[Lightbulb could not mirror one event: it exceeded the envelope]",
        original["ts"].as_str(),
        original["itemId"].as_str(),
    );
    marker["truncated"] = json!(true);
    // The ids are inherited, and a queue restored from an older build never saw admissible.
    admissible(&mut marker);
    marker
}

/// Which prompt a session is waiting on. One prompt can be reported twice --
/// `AskUserQuestion` fires `PreToolUse` AND `PermissionRequest`, in no
/// guaranteed order and with no guarantee both arrive -- so identity has to
/// survive one of the two showing up without a tool-use id.
///
/// `summary` is the REDACTED reason: a command line can hold a token, and this
/// is persisted to disk.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct WaitingPrompt {
    #[serde(default)]
    id: Option<String>,
    #[serde(default)]
    tool: Option<String>,
    #[serde(default)]
    summary: String,
    /// The hook is holding this prompt open for a remote answer.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    held: Option<approval::Held>,
}

impl WaitingPrompt {
    /// The same prompt reported twice, or a second prompt that reads alike?
    /// Tool-use ids settle it when both sides carry one -- the same command
    /// denied and asked again is two prompts. Otherwise the tool and what it is
    /// asking about do, which is what pairs a question's two hooks.
    fn is(&self, other: &Self) -> bool {
        if let (Some(mine), Some(theirs)) = (&self.held, &other.held) {
            return mine.id == theirs.id;
        }
        match (&self.id, &other.id) {
            (Some(mine), Some(theirs)) => mine == theirs,
            _ => self.tool == other.tool && self.summary == other.summary,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Mirror {
    #[serde(default)]
    workspace: workspace::State,
    native: NativeSession,
    generation: String,
    previous_generation: Option<String>,
    session_id: Option<String>,
    cursor: Cursor,
    after_seq: u64,
    queue: Vec<Value>,
    pending: Option<Value>,
    #[serde(default)]
    registration: RegistrationState,
    #[serde(default)]
    pub ended_at: Option<u64>,
    #[serde(default)]
    pub ended_at_ms: Option<u64>,
    #[serde(default)]
    resume_generation: Option<String>,
    #[serde(default)]
    resume_registration_pending: bool,
    #[serde(default)]
    deferred_end_reason: Option<String>,
    #[serde(default)]
    pub ended_reason: Option<String>,
    // The relay has acknowledged this session's end. Durable: a Mac that reports an
    // end and restarts must not report it again, and one that does not must.
    #[serde(default)]
    ended_sent: bool,
    // The prompt a waiting row has already been queued for. `None` means "not
    // waiting": every other transition clears it, so the same prompt asked again
    // after an answer is news rather than a repeat.
    #[serde(default)]
    waiting: Option<WaitingPrompt>,
    /// Model, permission mode and reported tokens, read off the same rows.
    #[serde(default)]
    stats: stats::Stats,
    /// This session's subagents, each tailed like the session itself and mirrored into
    /// the same event stream tagged with its id: they share its sharing, pause and end.
    #[serde(default, skip_serializing_if = "Subagents::is_empty")]
    subagents: Subagents,
    // RAM changes remain dirty across failed writes; this is never restored from disk.
    #[serde(skip)]
    dirty: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct Subagent {
    /// None until a Codex child's rollout is found (Claude's are found by their file).
    path: Option<PathBuf>,
    cursor: Cursor,
    /// Unreadable or inconsistent: stop tailing it. The parent is unaffected.
    #[serde(default)]
    closed: bool,
    /// What it was asked to do: its meta description, else the start of its first prompt.
    #[serde(default)]
    description: String,
    /// Its own end has been mirrored (a later row means it was resumed).
    #[serde(default)]
    ended: bool,
    /// The timestamp of its last row when that row read as a final answer (text, no tool
    /// call): Claude often writes the last one with no stop reason, so quiet after it ends it.
    #[serde(default)]
    final_at: Option<String>,
    /// Output tokens its own rows reported, for the "↓ tokens" figure.
    #[serde(default)]
    tokens: u64,
}

/// A subagent that answered and then wrote nothing for this long has ended.
const SUBAGENT_QUIET_SECS: u64 = 30;

/// The end a subagent's own files show, independent of anything its parent wrote, so a
/// reader whose window never held the parent's call or notification still sees it end.
fn own_end(id: &str, sub: &Subagent, ts: Option<&str>) -> Value {
    let what = if sub.description.is_empty() { "subagent" } else { &sub.description };
    let mut end = claude::subagent_end(id, &format!("Agent \"{what}\" finished"), "completed", false, None, ts, None);
    end["tool"]["input"] = json!({ "description": sub.description });
    if sub.tokens > 0 {
        end["tool"]["result"]["tokens"] = json!(sub.tokens);
    }
    end
}

/// A Claude assistant row that answers rather than calls a tool.
fn final_row(row: &Value) -> bool {
    let blocks = row["message"]["content"].as_array();
    row["type"] == "assistant"
        && blocks.is_some_and(|b| b.iter().any(|x| x["type"] == "text") && !b.iter().any(|x| x["type"] == "tool_use"))
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct Subagents {
    #[serde(flatten)]
    known: std::collections::BTreeMap<String, Subagent>,
    /// The relay stores subagent events (it listed `sessionSubagents`). Learned per
    /// connection; until then nothing tagged is read or sent.
    #[serde(skip)]
    relay: bool,
    /// Distinguish an older relay from a fresh connection that has not ACKed yet.
    #[serde(skip)]
    relay_known: bool,
}

impl Subagents {
    fn is_empty(&self) -> bool {
        self.known.is_empty()
    }
}

/// Codex subagents named by a `subagent_start` in `queue`, recorded for lookup.
fn announce(queue: &[Value], known: &mut std::collections::BTreeMap<String, Subagent>) {
    for e in queue {
        if let Some(id) = e["subagent"].as_str().filter(|_| e["lifecycle"] == "subagent_start") {
            known.entry(id.to_owned()).or_default();
        }
    }
}

/// Bound one normalized event to the wire and queue it (split when its text is long).
fn enqueue(queue: &mut Vec<Value>, mut event: Value) {
    admissible(&mut event);
    let text = event["text"].as_str().unwrap_or("");
    if text.len() > MAX_TEXT {
        let mut end = MAX_TEXT;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        event["text"] = json!(&text[..end]);
        event["truncated"] = json!(true);
    }
    queue.extend(split_text(&event));
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
struct RegistrationState {
    // Last durable server ACK, not the last locally observed process.
    acknowledged: Option<String>,
    // Freezes Mirror.generation + previous_generation before any register send.
    pending: bool,
    // At most one latest observation while the frozen transition/history drains.
    successor: Option<NativeSession>,
}

impl Mirror {
    fn persist(&mut self, path: &Path) -> Result<()> {
        self.dirty = true;
        private_write(path, self)?;
        self.dirty = false;
        Ok(())
    }
    fn registered(&mut self) {
        self.registration.acknowledged = Some(self.generation.clone());
        self.registration.pending = false;
        self.previous_generation = None;
        self.resume_registration_pending = false;
    }
    fn advance(&mut self) -> bool {
        if self.registration.pending || self.pending.is_some() || !self.queue.is_empty() {
            return false;
        }
        if let Some(generation) = self.resume_generation.take() {
            self.previous_generation = self.registration.acknowledged.clone();
            self.generation = generation;
            self.resume_registration_pending = true;
            return true;
        }
        let Some(native) = self.registration.successor.take() else {
            return false;
        };
        self.previous_generation = self.registration.acknowledged.clone();
        self.generation = native.process.generation();
        self.native = native;
        true
    }
    fn next_batch(&mut self) -> Result<Option<Value>> {
        if !self.subagents.relay {
            self.queue.retain(|e| e.get("subagent").is_none());
            if self.pending.as_ref().is_some_and(|batch| {
                batch["events"]
                    .as_array()
                    .is_some_and(|events| events.iter().any(|event| event.get("subagent").is_some()))
            }) {
                // The relay's feature set can change across reconnects. A cached
                // child batch from the previous socket must not replay to an older
                // relay; rebuild from the queue after filtering unsupported events.
                self.pending = None;
            }
        }
        if let Some(pending) = &self.pending {
            return Ok(Some(pending.clone()));
        }
        if self.queue.is_empty() && self.native.restriction.as_deref() != Some("history-gap") {
            // An ended session is exempt: its process is gone by definition, and the
            // last thing it wrote -- the end of the conversation -- is still unread.
            if self.ended_at.is_none()
                && !process(self.native.process.pid).is_ok_and(|p| p == self.native.process)
            {
                return Err("runtime-unverified".into());
            }
            let Some(path) = &self.native.transcript else {
                return Ok(None);
            };
            let mut cursor = self.cursor.clone();
            let mut stats = self.stats.clone();
            let mut queue = Vec::new();
            for row in cursor.read(path)? {
                stats.observe(&self.native.provider, &row);
                let events = normalize_public_message(
                    &self.native.provider,
                    &self.native.conversation,
                    &row,
                )?;
                for event in events {
                    enqueue(&mut queue, event);
                }
            }
            self.cursor = cursor;
            self.stats = stats;
            if self.subagents.relay {
                self.read_subagents(path.clone(), &mut queue);
            }
            self.queue = queue;
        }
        let Some(session_id) = &self.session_id else {
            return Ok(None);
        };
        if !self.subagents.relay {
            // An older relay rejects child events; apply the filter after reading
            // new rows as well as before returning a durable pending batch.
            self.queue.retain(|e| e.get("subagent").is_none());
        }
        if self.queue.is_empty() {
            return Ok(None);
        }
        let mut events: Vec<Value> = vec![];
        let mut batch = json!({"type":"local.session.events", "sessionId":session_id, "generation":self.generation,
            "batchId":uuid::Uuid::new_v4().to_string(), "afterSeq":self.after_seq, "events":[]});
        for candidate in self.queue.iter().take(32) {
            events.push(candidate.clone());
            batch["events"] = Value::Array(events.clone());
            if ascii_wire_len(&batch)? > 16384 {
                events.pop();
                batch["events"] = Value::Array(events.clone());
                break;
            }
        }
        if events.is_empty() {
            // A single event that cannot fit alone would replay forever (the batch fails,
            // the session re-registers, the persisted queue restores it). Mirror a marker
            // in its place and keep going.
            let marker = unsendable(&self.queue[0]);
            self.queue[0] = marker.clone();
            events.push(marker);
            batch["events"] = Value::Array(events);
        }
        self.pending = Some(batch.clone());
        Ok(Some(batch))
    }
    /// Find new subagents and read the next rows of each, tagged with its id. One that
    /// fails is closed on its own; nothing it does can gap or unverify the parent.
    fn read_subagents(&mut self, transcript: PathBuf, queue: &mut Vec<Value>) {
        let native = &self.native;
        let known = &mut self.subagents.known;
        if native.provider == "claude" {
            for (id, path, start) in claude::new_subagents(&transcript, &|id| known.contains_key(id)) {
                let description = start["text"].as_str().unwrap_or("").to_owned();
                enqueue(queue, start);
                known.insert(id, Subagent { path: Some(path), description, ..Default::default() });
            }
        } else {
            // A Codex child is announced by its parent's (or a child's) `started` activity;
            // one announced by a row read below is looked up on the next read.
            announce(queue, known);
            let mut found = vec![];
            for (id, sub) in known.iter_mut().filter(|(_, s)| s.path.is_none() && !s.closed) {
                if let Some((path, start)) = codex::child_rollout(&transcript, &native.conversation, id) {
                    sub.path = Some(path);
                    sub.description = start["text"].as_str().unwrap_or("").to_owned();
                    found.push(start);
                }
            }
            for start in found {
                enqueue(queue, start);
            }
        }
        for (id, sub) in known.iter_mut().filter(|(_, s)| !s.closed) {
            let Some(path) = &sub.path else { continue };
            // One stat, not a confined open, for the (usual) subagent with nothing new.
            let meta = fs::metadata(path);
            if meta.as_ref().is_ok_and(|m| m.len() == sub.cursor.offset) {
                let quiet = meta.ok().and_then(|m| m.modified().ok()).and_then(|t| t.elapsed().ok())
                    .is_some_and(|age| age.as_secs() >= SUBAGENT_QUIET_SECS);
                if !sub.ended && quiet {
                    if let Some(at) = sub.final_at.take() {
                        enqueue(queue, own_end(id, sub, Some(&at)));
                        sub.ended = true;
                    }
                }
                continue;
            }
            // A Claude subagent writes its parent's session id; a Codex child its own thread's.
            let session = if native.provider == "claude" { &native.conversation } else { id };
            let mut cursor = sub.cursor.clone();
            let read = cursor.read(path).and_then(|rows| {
                let events = rows.iter().try_fold(vec![], |mut all, row| {
                    all.extend(normalize_public_message(&native.provider, session, row)?);
                    Ok::<_, String>(all)
                })?;
                Ok((rows, events))
            });
            match read {
                Ok((rows, events)) => {
                    sub.cursor = cursor;
                    // Claude repeats a message's usage on each of its block rows; count it once per message id.
                    let mut seen = std::collections::BTreeSet::new();
                    for row in &rows {
                        let usage = &row["message"]["usage"]["output_tokens"];
                        if let (Some(n), Some(m)) = (usage.as_u64(), row["message"]["id"].as_str()) {
                            if seen.insert(m.to_owned()) {
                                sub.tokens += n;
                            }
                        }
                    }
                    if let Some(last) = rows.last() {
                        sub.final_at = final_row(last).then(|| last["timestamp"].as_str().unwrap_or("").to_owned()).filter(|t| !t.is_empty());
                    }
                    if sub.description.is_empty() {
                        // No meta description: what it was first asked, the way the TUI labels it.
                        if let Some(ask) = events.iter().find(|e| e["kind"] == "text" && e["role"] == "user").and_then(|e| e["text"].as_str()) {
                            sub.description = ask.split_whitespace().collect::<Vec<_>>().join(" ").chars().take(80).collect();
                            let mut start = lifecycle("subagent_start", &sub.description, None, None);
                            start["subagent"] = json!(id);
                            enqueue(queue, start);
                        }
                    }
                    let own = |e: &Value| e.get("subagent").is_none();
                    let answered = events.iter().rev().find(|e| own(e)).is_some_and(|e| e["lifecycle"] == "turn_end");
                    if !events.is_empty() {
                        sub.ended = false;
                    }
                    let end_ts = events.last().and_then(|e| e["ts"].as_str()).map(str::to_owned);
                    for mut event in events {
                        // A nested subagent's own start or end already names that child.
                        if own(&event) {
                            event["subagent"] = json!(id);
                        }
                        enqueue(queue, event);
                    }
                    if answered {
                        enqueue(queue, own_end(id, sub, end_ts.as_deref()));
                        sub.ended = true;
                        sub.final_at = None;
                    }
                }
                Err(_) => sub.closed = true,
            }
        }
        if native.provider != "claude" {
            announce(queue, known);
        }
    }
    /// Returns `Ok(true)` if this ack advanced state, `Ok(false)` if it was a stale
    /// duplicate safely ignored (e.g. the relay answering a re-sent batch twice).
    fn acknowledge(&mut self, batch_id: &str, after_seq: u64) -> Result<bool> {
        let Some(pending) = self.pending.as_ref() else {
            return if after_seq <= self.after_seq {
                Ok(false)
            } else {
                Err("unexpected upload acknowledgment".into())
            };
        };
        if pending["batchId"] != batch_id {
            // A duplicate ack for an earlier, already-applied batch can arrive after a
            // different batch is already pending; ignore it rather than conflict.
            return if after_seq <= self.after_seq {
                Ok(false)
            } else {
                Err(format!(
                    "upload cursor conflict: ack for another batch at {after_seq} beyond local {}",
                    self.after_seq
                ))
            };
        }
        let count = pending["events"].as_array().map(Vec::len).unwrap_or(0) as u64;
        if after_seq != self.after_seq + count {
            return Err(format!(
                "upload cursor conflict: acked at {after_seq}, expected {}",
                self.after_seq + count
            ));
        }
        self.after_seq = after_seq;
        self.queue.drain(..count as usize);
        self.pending = None;
        Ok(true)
    }
    /// Adopt the relay's cursor when neither "nothing applied" nor "the pending batch was
    /// applied" explains it. The local backlog is dropped; one system marker tells viewers.
    fn resync(&mut self, after: u64) {
        self.after_seq = after;
        self.pending = None;
        self.queue.clear();
        self.queue.push(json!({
            "kind": "text",
            "role": "system",
            "text": "[Lightbulb could not mirror some output: this Mac and the relay disagreed about the transcript cursor]",
            "truncated": false
        }));
    }
}

fn ascii_wire_len(value: &Value) -> Result<usize> {
    // The backend uses json.dumps(ensure_ascii=True); count surrogate pairs too.
    Ok(serde_json::to_string(value)
        .map_err(|e| e.to_string())?
        .chars()
        .map(|c| if c.is_ascii() { 1 } else { c.len_utf16() * 6 })
        .sum())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        io::Write,
        os::unix::fs::{symlink, PermissionsExt},
    };

    fn command() -> Command {
        serde_json::from_value(json!({"requestId":"r1", "expectedGeneration":"g1", "kind":"send", "text":"hello",
            "expectedTurnId":null, "sessionId":"s1", "roomId":"room", "actorUserId":"alice", "commandSeq":1,
            "state":"dispatched", "createdAt":100, "deliverBy":130, "held":false, "providerMessageId":null})).unwrap()
    }

    #[test]
    fn local_sessions_agent_wire_expiry_and_legacy_human_hash() {
        let human = command();
        let wire = serde_json::to_string(&human).unwrap();
        assert_eq!(
            wire,
            r#"{"requestId":"r1","expectedGeneration":"g1","kind":"send","text":"hello","expectedTurnId":null,"sessionId":"s1","roomId":"room","actorUserId":"alice","commandSeq":1,"state":"dispatched","createdAt":100,"deliverBy":130,"held":false,"providerMessageId":null}"#
        );
        let mut agent = serde_json::to_value(&human).unwrap();
        agent.as_object_mut().unwrap().remove("actorUserId");
        agent["actor"] = json!({"kind":"agent","id":"friday"});
        agent["invocationExpiresAt"] = json!(115);
        let cmd: Command = serde_json::from_value(agent.clone()).unwrap();
        let dir = tempfile::tempdir().unwrap();
        let journal = Journal {
            dir: dir.path().into(),
        };
        assert!(journal.admit(&cmd, "g1", 110).unwrap());
        let dir = tempfile::tempdir().unwrap();
        assert!(!Journal {
            dir: dir.path().into()
        }
        .admit(&cmd, "g1", 115)
        .unwrap());
        agent["actorUserId"] = json!("alice");
        let invalid: Command = serde_json::from_value(agent).unwrap();
        let dir = tempfile::tempdir().unwrap();
        assert!(!Journal {
            dir: dir.path().into()
        }
        .admit(&invalid, "g1", 110)
        .unwrap());
    }

    #[test]
    fn local_sessions_journal_anchors_only_future_ingress_and_preserves_terminal_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("history.jsonl");
        fs::write(&transcript, "{\"old\":true}\n").unwrap();
        let journal = Journal {
            dir: dir.path().into(),
        };
        let cmd = command();
        assert!(journal.admit(&cmd, "g1", 110).unwrap());
        let native = NativeSession {
            provider: "claude".into(),
            conversation: "s".into(),
            profile: "p".into(),
            process: process(std::process::id()).unwrap(),
            transcript: Some(transcript.clone()),
            socket: None,
            socket_inode: None,
            version: "2.1.268".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            background: false,
        };
        let mut workspace = workspace::State::default();
        workspace.observe(Ok((dir.path().into(), "before".into())), false);
        workspace.observe(Ok((dir.path().into(), "external-edit".into())), true);
        let exact = format!(
            "[Lightbulb teammate contribution {}]\n{}{}",
            cmd.provenance(),
            workspace.notice(),
            cmd.text.as_deref().unwrap()
        );
        journal
            .anchor(&cmd, &native, exact.clone(), Some(workspace.revision()))
            .unwrap();
        let receipt = journal.reconcile("s1", "r1", "g1").unwrap();
        assert_eq!(receipt.ingress.as_ref().unwrap().text, exact);
        assert_eq!(
            receipt.ingress.as_ref().unwrap().workspace_revision,
            Some(workspace.revision())
        );
        assert_eq!(
            receipt.ingress.unwrap().cursor.offset,
            fs::metadata(transcript).unwrap().len()
        );
        assert_eq!(
            journal
                .finish("s1", "r1", Some("native-id".into()), true)
                .unwrap()
                .outcome,
            "applied"
        );
        assert_eq!(
            journal.finish("s1", "r1", None, false).unwrap().outcome,
            "applied"
        );
    }

    #[test]
    fn local_sessions_journal_fences_duplicates_restart_deadlines_and_missing_receipts() {
        let dir = tempfile::tempdir().unwrap();
        let journal = Journal {
            dir: dir.path().into(),
        };
        let cmd = command();
        assert!(journal.admit(&cmd, "g1", 110).unwrap());
        assert!(!journal.admit(&cmd, "g1", 111).unwrap());
        let reopened = Journal {
            dir: dir.path().into(),
        };
        assert!(!reopened.admit(&cmd, "g1", 112).unwrap());
        assert_eq!(
            reopened.reconcile("s1", "r1", "g1").unwrap().outcome,
            "unknown"
        );
        let mut changed = cmd.clone();
        changed.text = Some("different".into());
        assert!(reopened.admit(&changed, "g1", 112).is_err());
        changed.request_id = "late".into();
        assert!(!reopened.admit(&changed, "g1", 130).unwrap());
        assert!(!reopened.admit(&changed, "g1", 110).unwrap());
        changed.request_id = "next".into();
        changed.command_seq = 2;
        assert!(
            !reopened.admit(&changed, "g1", 110).unwrap(),
            "an unresolved native intent blocks subsequent sends"
        );
        changed.request_id = "old-generation".into();
        assert!(!reopened.admit(&changed, "g2", 110).unwrap());
        assert_eq!(
            reopened.reconcile("s1", "unseen", "g1").unwrap().outcome,
            "rejected"
        );
        changed.request_id = "unseen".into();
        assert!(!reopened.admit(&changed, "g1", 110).unwrap());
    }

    #[test]
    fn local_sessions_journal_native_successor_bypasses_only_old_generation_unknown() {
        let dir = tempfile::tempdir().unwrap();
        let journal = Journal {
            dir: dir.path().into(),
        };
        let old = command();
        assert!(journal.admit(&old, "g1", 110).unwrap());
        let mut next = old.clone();
        next.command_seq = 2;
        next.request_id = "same-process".into();
        assert!(!journal.admit(&next, "g1", 111).unwrap());
        let restored = Journal {
            dir: dir.path().into(),
        };
        next.command_seq = 3;
        next.request_id = "new-process".into();
        next.expected_generation = "g2".into();
        assert!(restored.admit(&next, "g2", 112).unwrap());
        assert!(!restored.admit(&old, "g2", 112).unwrap());
        assert_eq!(
            restored.reconcile("s1", "r1", "g1").unwrap().outcome,
            "unknown"
        );
        let mut conflicting = old.clone();
        conflicting.expected_generation = "g2".into();
        assert!(restored.admit(&conflicting, "g2", 112).is_err());
        next.request_id = "stale-process".into();
        next.command_seq = 4;
        next.expected_generation = "g1".into();
        assert!(!restored.admit(&next, "g2", 112).unwrap());
        next.request_id = "out-of-order".into();
        next.command_seq = 2;
        next.expected_generation = "g3".into();
        assert!(!restored.admit(&next, "g3", 112).unwrap());
    }

    #[test]
    fn local_sessions_cursor_retains_partial_records_and_refuses_rotation_or_truncation() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.jsonl");
        fs::write(&path, b"{\"id\":1}\n{\"id\":").unwrap();
        let mut cursor = Cursor::default();
        assert_eq!(cursor.read(&path).unwrap(), vec![json!({"id":1})]);
        assert!(cursor.read(&path).unwrap().is_empty());
        fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(b"2}\n")
            .unwrap();
        assert_eq!(cursor.read(&path).unwrap(), vec![json!({"id":2})]);
        let mut rotated = cursor.clone();
        fs::write(&path, b"{}\n").unwrap();
        assert!(cursor.read(&path).is_err());
        fs::rename(&path, dir.path().join("old.jsonl")).unwrap();
        fs::write(&path, b"{\"id\":1}\n{\"id\":2}\n").unwrap();
        assert!(rotated.read(&path).is_err());
    }

    #[test]
    fn local_sessions_cursor_reads_large_valid_rows_but_keeps_its_bound() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("history.jsonl");
        let command = json!({"type":"event_msg","payload":{"type":"item_completed",
            "item":{"type":"CommandExecution","aggregated_output":"x".repeat(2_200_000)}}});
        let compacted = json!({"type":"compacted","payload":{"replacement_history":"y".repeat(3_300_000)}});
        let oversized = json!({"type":"compacted","payload":{"replacement_history":"z".repeat(MAX_JSONL_ROW)}});
        fs::write(&path, format!("{command}\n{compacted}\n{oversized}\n")).unwrap();
        let mut cursor = Cursor::default();
        assert_eq!(cursor.read(&path).unwrap()[0]["payload"]["item"]["type"], "CommandExecution");
        assert_eq!(cursor.read(&path).unwrap()[0]["type"], "compacted");
        let safe_offset = cursor.offset;
        assert_eq!(cursor.read(&path).unwrap_err(), "history-gap");
        assert_eq!(cursor.offset, safe_offset, "an oversized row is never skipped");
    }

    #[test]
    fn local_sessions_private_state_rejects_symlinks_and_is_owner_only() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("state.json");
        private_write(&path, &json!({"text":"private"})).unwrap();
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let link = dir.path().join("link.json");
        symlink(&path, &link).unwrap();
        assert!(private_write(&link, &json!({"text":"changed"})).is_err());
        assert_eq!(
            serde_json::from_slice::<Value>(&fs::read(&path).unwrap()).unwrap()["text"],
            "private"
        );
        let nested = dir.path().join("nested");
        symlink(dir.path(), &nested).unwrap();
        assert!(secure_file(&nested.join("state.json")).is_err());
        assert!(private_write(&nested.join("new.json"), &json!({"secret":"never"})).is_err());
    }

    #[test]
    fn local_sessions_logout_pauses_consent_and_never_moves_workspace() {
        let mut enrollment = Enrollment {
            server_revision: None,
            remote_disabled: false,
            attempt: None,
            owner_id: "org1".into(),
            human_user_id: "alice".into(),
            device_id: "d".into(),
            gateway_url: "wss://example.invalid".into(),
            enabled: true,
            paused: false,
        };
        enrollment.authenticate(Some(None));
        assert!(enrollment.enabled && enrollment.paused);
        enrollment.authenticate(Some(Some("bob")));
        assert!(enrollment.paused);
        enrollment.authenticate(Some(Some("alice")));
        assert!(!enrollment.paused);
        assert_eq!(enrollment.owner_id, "org1");
    }

    #[test]
    fn local_sessions_process_birth_is_real_and_stale_pids_fail() {
        let actual = process(std::process::id()).unwrap();
        assert_eq!(actual.pid, std::process::id());
        assert!(actual.birth_micros > 0);
        assert!(actual.executable.is_absolute());
        assert_eq!(process(std::process::id()).unwrap(), actual);
        assert!(process(u32::MAX).is_err());
        // Another name for the same process is the same process and generation.
        let mut renamed = actual.clone();
        renamed.executable = "/elsewhere/claude".into();
        assert_eq!(renamed, actual);
        assert_eq!(renamed.generation(), actual.generation());
        let mut restarted = actual.clone();
        restarted.birth_micros += 1;
        assert_ne!(restarted, actual);
        assert_ne!(restarted.generation(), actual.generation());
    }

    #[test]
    fn local_sessions_profile_roots_reject_escapes_and_symlink_substitution() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let path = root.join("history.jsonl");
        fs::write(&path, b"{}\n").unwrap();
        assert_eq!(confined_path(&root, &path).unwrap(), path);
        assert!(confined_path(&root, &root.join("../elsewhere")).is_err());
        let linked = root.join("linked");
        symlink(&root, &linked).unwrap();
        assert!(confined_path(&root, &linked.join("history.jsonl")).is_err());
    }

    #[test]
    fn local_sessions_app_bundle_is_the_installed_binary_but_a_lookalike_is_not() {
        // Mirrors the real layout: `<root>/versions/<v>` is what PATH resolves to and
        // `<root>/ClaudeCode.app/Contents/MacOS/claude` is a hard link onto it.
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let installed = root.join("versions/2.1.273");
        fs::create_dir_all(root.join("versions")).unwrap();
        fs::write(&installed, b"binary").unwrap();
        let bundle = root.join("ClaudeCode.app/Contents/MacOS/claude");
        fs::create_dir_all(bundle.parent().unwrap()).unwrap();
        fs::hard_link(&installed, &bundle).unwrap();

        assert_eq!(same_installed_file(&installed, &installed), Ok(()));
        assert_eq!(
            same_installed_file(&bundle, &installed),
            Ok(()),
            "the install's other name for the same inode is the same binary"
        );

        // Same bytes, different file, inside the root: still not the installed binary.
        let copy = root.join("ClaudeCode.app/Contents/MacOS/claude-copy");
        fs::write(&copy, b"binary").unwrap();
        assert!(same_installed_file(&copy, &installed).is_err());

        // A hard link to the same inode from outside the root is refused on the root.
        let away = tempfile::tempdir().unwrap();
        let outside = fs::canonicalize(away.path()).unwrap();
        let elsewhere = outside.join("claude");
        fs::hard_link(&installed, &elsewhere).unwrap();
        assert!(same_installed_file(&elsewhere, &installed).is_err());

        // And a symlink that leaves the root cannot smuggle one back in.
        let escape = root.join("escape");
        symlink(&outside, &escape).unwrap();
        assert!(same_installed_file(&escape.join("claude"), &installed).is_err());
    }

    #[test]
    fn local_sessions_discovery_deduplicates_running_sessions_and_replaces_pid_reuse() {
        let native = NativeSession {
            provider: "codex".into(),
            conversation: "thread".into(),
            profile: "root".into(),
            process: Process {
                pid: 42,
                birth_micros: 100,
                executable: "/bin/codex".into(),
            },
            transcript: None,
            socket: None,
            socket_inode: None,
            version: "0.149.0".into(),
            restriction: None,
            status: "unknown".into(),
            title: None,
            cwd_name: None,
            active_turn: None,
            last_assistant_message: None,
            background: false,
        };
        let mut sessions = vec![];
        reconcile_sessions(&mut sessions, vec![native.clone(), native.clone()]);
        assert_eq!(sessions.len(), 1);
        reconcile_sessions(&mut sessions, vec![native.clone()]);
        assert_eq!(sessions.len(), 1);
        let mut restarted = native;
        restarted.process.birth_micros = 200;
        reconcile_sessions(&mut sessions, vec![restarted.clone()]);
        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].process.birth_micros, 200);
        let mut concurrent = restarted.clone();
        concurrent.process.pid = 43;
        reconcile_sessions(&mut sessions, vec![restarted, concurrent]);
        assert_eq!(
            sessions[0].restriction.as_deref(),
            Some("runtime-unverified"),
            "two live runtimes for one native identity are ambiguous"
        );
        reconcile_sessions(&mut sessions, vec![]);
        assert_eq!(sessions.len(), 1);
        assert_eq!(
            sessions[0].status, "unknown",
            "an unlisted session keeps its status; hooks and the pid check end it"
        );
    }

    #[test]
    fn local_sessions_deadline_uses_server_time_and_invalidates_sleep_or_clock_jumps() {
        assert_eq!(deadline_now(100, 0.8, 2.1, Some(2.1)), Some(104));
        assert_eq!(deadline_now(100, 0.8, 2.1, Some(300.0)), None);
        assert_eq!(deadline_now(100, 0.8, 2.1, None), None);
        assert_eq!(deadline_now(100, 0.8, 31.0, Some(31.0)), None);
        assert_eq!(
            deadline_now(100, 0.1, 1.0, Some(2.5)),
            Some(104),
            "short suspend/jitter cannot make the bound younger"
        );
    }

    #[test]
    fn local_sessions_utf8_batches_bound_actual_json_without_losing_text() {
        let input = "é😀\\\"".repeat(2000);
        let events = split_text(
            &json!({"kind":"text", "role":"assistant", "text":input, "truncated":false}),
        );
        assert!(events.len() > 1);
        let joined = events
            .iter()
            .map(|e| e["text"].as_str().unwrap())
            .collect::<String>();
        assert_eq!(joined, input);
        for e in events {
            assert!(serde_json::to_string(&e).unwrap().len() < 4096);
        }
    }

    #[test]
    fn local_sessions_mirror_retries_the_same_batch_across_restart_and_advances_only_on_ack() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        fs::write(&transcript, "{\"type\":\"assistant\",\"sessionId\":\"s\",\"version\":\"2.1.268\",\"uuid\":\"m\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}}\n").unwrap();
        let mut mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: NativeSession {
                provider: "claude".into(),
                conversation: "s".into(),
                profile: "root".into(),
                process: process(std::process::id()).unwrap(),
                transcript: Some(transcript),
                socket: None,
                socket_inode: None,
                version: "2.1.268".into(),
                restriction: None,
                status: "unknown".into(),
                title: None,
                cwd_name: None,
                active_turn: None,
                last_assistant_message: None,
                background: false,
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        let first = mirror.next_batch().unwrap().unwrap();
        assert_eq!(first["events"][0]["text"], "hello");
        assert_eq!(first["afterSeq"], 0);
        let state = dir.path().join("mirror.json");
        private_write(&state, &mirror).unwrap();
        let mut restored: Mirror = private_read(&state).unwrap();
        assert_eq!(restored.next_batch().unwrap().unwrap(), first);
        assert!(restored.acknowledge("wrong-batch", 1).is_err());
        restored
            .acknowledge(first["batchId"].as_str().unwrap(), 1)
            .unwrap();
        assert_eq!(restored.after_seq, 1);
        assert!(restored.next_batch().unwrap().is_none());
        let mut file = fs::OpenOptions::new()
            .append(true)
            .open(restored.native.transcript.as_ref().unwrap())
            .unwrap();
        writeln!(file,"{}",json!({"type":"assistant","sessionId":"s","version":"2.1.268","message":{"role":"assistant","content":[{"type":"text","text":"final before exit"}]}})).unwrap();
        restored.ended_at = Some(1);
        restored.native.process.pid = u32::MAX;
        assert_eq!(
            restored.next_batch().unwrap().unwrap()["events"][0]["text"],
            "final before exit",
            "an enrolled transcript's anchored final records survive process exit"
        );
    }

    #[test]
    fn local_sessions_split_text_preserves_empty_marker_events_and_bounds_tool_text_without_splitting(
    ) {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        let ts = "2026-09-12T10:00:00.000Z";
        let turn = json!({"type":"assistant","sessionId":"s","version":"2.1.269","uuid":"a1","timestamp":ts,
            "message":{"role":"assistant","stop_reason":"end_turn","content":[{"type":"thinking","thinking":"","signature":"x"},{"type":"text","text":"All good."}]}});
        let empty_result = json!({"type":"user","sessionId":"s","version":"2.1.269","uuid":"u1","timestamp":ts,
            "toolUseResult":{},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"","is_error":false}]}});
        let big = "x".repeat(30 * 1024);
        let big_result = json!({"type":"user","sessionId":"s","version":"2.1.269","uuid":"u2","timestamp":ts,
            "toolUseResult":{},"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_2","content":big,"is_error":false}]}});
        fs::write(
            &transcript,
            format!("{turn}\n{empty_result}\n{big_result}\n"),
        )
        .unwrap();
        let mut mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: NativeSession {
                provider: "claude".into(),
                conversation: "s".into(),
                profile: "root".into(),
                process: process(std::process::id()).unwrap(),
                transcript: Some(transcript),
                socket: None,
                socket_inode: None,
                version: "2.1.269".into(),
                restriction: None,
                status: "unknown".into(),
                title: None,
                cwd_name: None,
                active_turn: None,
                last_assistant_message: None,
                background: false,
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        mirror.next_batch().unwrap();
        assert!(
            mirror.queue.iter().any(|e| e["kind"] == "thinking"),
            "thinking marker survived"
        );
        assert!(
            mirror
                .queue
                .iter()
                .any(|e| e["kind"] == "lifecycle" && e["lifecycle"] == "turn_end"),
            "turn_end lifecycle survived"
        );
        assert!(
            mirror
                .queue
                .iter()
                .any(|e| e["role"] == "tool" && e["text"] == ""),
            "empty tool result survived"
        );
        let big_events: Vec<_> = mirror
            .queue
            .iter()
            .filter(|e| e["text"].as_str().is_some_and(|t| t.len() == 8192))
            .collect();
        assert_eq!(
            big_events.len(),
            1,
            "the big tool result is bounded, not split into several chunks"
        );
        assert_eq!(big_events[0]["truncated"], true);
    }

    #[test]
    fn local_sessions_mirror_packs_many_events_per_envelope_and_splits_by_wire_length() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        let row = |i: usize| {
            json!({"type":"assistant","sessionId":"s","version":"2.1.268","uuid":format!("m{i}"),"message":{"role":"assistant","content":[{"type":"text","text":format!("event {i}")}]}}).to_string()
        };
        fs::write(
            &transcript,
            (0..40).map(row).collect::<Vec<_>>().join("\n") + "\n",
        )
        .unwrap();
        let mut mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: NativeSession {
                provider: "claude".into(),
                conversation: "s".into(),
                profile: "root".into(),
                process: process(std::process::id()).unwrap(),
                transcript: Some(transcript),
                socket: None,
                socket_inode: None,
                version: "2.1.268".into(),
                restriction: None,
                status: "unknown".into(),
                title: None,
                cwd_name: None,
                active_turn: None,
                last_assistant_message: None,
                background: false,
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        let first = mirror.next_batch().unwrap().unwrap();
        let n = first["events"].as_array().unwrap().len();
        assert!(n > 1 && n <= 32);
        assert!(ascii_wire_len(&first).unwrap() <= 16384);
        assert!(
            mirror
                .acknowledge(first["batchId"].as_str().unwrap(), 1)
                .is_err(),
            "must advance by the batch length"
        );
        mirror
            .acknowledge(first["batchId"].as_str().unwrap(), n as u64)
            .unwrap();
        assert_eq!(mirror.after_seq, n as u64);
        let parts = split_text(
            &json!({"kind":"text","role":"assistant","text":"é".repeat(6000),"truncated":false,"itemId":"m"}),
        );
        assert!(parts.len() > 1);
        // 11_000 of text budget leaves room for the other fields inside a 12_000 wire budget per event.
        assert!(parts
            .iter()
            .all(|p| p["itemId"] == "m" && ascii_wire_len(p).unwrap() <= 12_000));
        assert_eq!(
            split_text(&json!({"kind":"thinking","role":"assistant","text":"","truncated":false}))
                .len(),
            1,
            "empty markers still travel"
        );
        let tool = split_text(
            &json!({"kind":"tool","role":"tool","text":"x".repeat(20_000),"truncated":false,"tool":{"name":"result","status":"done"}}),
        );
        assert_eq!(tool.len(), 1, "tool events are bounded, never chunked");
        assert!(tool[0]["truncated"] == true && tool[0]["text"].as_str().unwrap().len() == 8192);
        assert_eq!(
            parts
                .iter()
                .map(|p| p["text"].as_str().unwrap())
                .collect::<String>(),
            "é".repeat(6000)
        );
    }

    #[test]
    fn local_sessions_mirror_next_batch_stops_at_wire_budget_and_preserves_the_rest() {
        // Six ~5 KiB ASCII rows: only 3 fit inside a 16 KiB envelope (15406 bytes at
        // 3, 20488 at 4), forcing next_batch's push -> check -> pop -> re-clone branch.
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        let row_text = |i: usize| format!("row{i}-{}", "y".repeat(4990));
        let row = |i: usize| {
            json!({"type":"assistant","sessionId":"s","version":"2.1.268","uuid":format!("m{i}"),
                "message":{"role":"assistant","content":[{"type":"text","text":row_text(i)}]}})
            .to_string()
        };
        fs::write(
            &transcript,
            (0..6).map(row).collect::<Vec<_>>().join("\n") + "\n",
        )
        .unwrap();
        let mut mirror = Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: NativeSession {
                provider: "claude".into(),
                conversation: "s".into(),
                profile: "root".into(),
                process: process(std::process::id()).unwrap(),
                transcript: Some(transcript),
                socket: None,
                socket_inode: None,
                version: "2.1.268".into(),
                restriction: None,
                status: "unknown".into(),
                title: None,
                cwd_name: None,
                active_turn: None,
                last_assistant_message: None,
                background: false,
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        };
        let first = mirror.next_batch().unwrap().unwrap();
        let n = first["events"].as_array().unwrap().len();
        assert!(ascii_wire_len(&first).unwrap() <= 16384);
        assert!(
            n < 6,
            "six ~5 KiB rows must not all fit in one 16 KiB envelope"
        );
        assert_eq!(
            mirror.queue.len(),
            6,
            "next_batch must not drop the events it declined to pack"
        );
        // The event the loop pushed then popped for overflowing must be untouched
        // in the queue, ready for the next envelope.
        let popped_text = mirror.queue[n]["text"].as_str().unwrap().to_string();
        mirror
            .acknowledge(first["batchId"].as_str().unwrap(), n as u64)
            .unwrap();
        assert_eq!(mirror.queue.len(), 6 - n);
        let second = mirror.next_batch().unwrap().unwrap();
        assert_eq!(second["events"][0]["text"], popped_text);
    }

    fn test_mirror(transcript: PathBuf) -> Mirror {
        Mirror {
            workspace: workspace::State::default(),
            dirty: false,
            registration: RegistrationState::default(),
            native: NativeSession {
                provider: "claude".into(),
                conversation: "s".into(),
                profile: "root".into(),
                process: process(std::process::id()).unwrap(),
                transcript: Some(transcript),
                socket: None,
                socket_inode: None,
                version: "2.1.269".into(),
                restriction: None,
                status: "unknown".into(),
                title: None,
                cwd_name: None,
                active_turn: None,
                last_assistant_message: None,
                background: false,
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
            ended_at: None,
            ended_at_ms: None,
            resume_generation: None,
            resume_registration_pending: false,
            deferred_end_reason: None,
            ended_reason: None,
            ended_sent: false,
            waiting: None,
            stats: Default::default(),
            subagents: Default::default(),
        }
    }

    #[test]
    fn local_sessions_ack_for_an_already_reconciled_batch_is_a_no_op() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(dir.path().join("transcript.jsonl"));
        mirror.after_seq = 7;
        // The registration ACK already reconciled this batch; its own ACK arrives late.
        assert_eq!(mirror.acknowledge("b1", 7), Ok(false));
        assert_eq!(mirror.after_seq, 7);
        assert!(mirror.pending.is_none());
        assert!(mirror.acknowledge("b1", 8).is_err(), "a cursor we never reached is still a conflict");
    }
    #[test]
    fn local_sessions_acknowledge_ignores_stale_duplicate_batch_acks_but_conflicts_on_real_mismatches(
    ) {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(dir.path().join("transcript.jsonl"));

        // No pending batch, cursor > local: unexplained.
        mirror.after_seq = 5;
        assert!(mirror.acknowledge("stale", 6).is_err());

        // A batch is outstanding under a different batchId than the one arriving.
        let events = vec![json!({"kind":"text","role":"assistant","text":"e","truncated":false})];
        mirror.queue = events.clone();
        mirror.pending = Some(json!({"batchId":"b2","events":events}));

        // Duplicate ack for an earlier, already-applied batchId at/under the local
        // cursor: ignored as stale, no state change.
        assert_eq!(mirror.acknowledge("b1", 5), Ok(false));
        assert_eq!(mirror.after_seq, 5);
        assert!(mirror.pending.is_some(), "the still-outstanding batch is untouched");
        assert_eq!(mirror.queue.len(), 1);

        // Different batchId, cursor beyond local: unexplained, not stale.
        assert!(mirror.acknowledge("b1", 6).is_err());

        // Right batchId, wrong cursor: a real conflict.
        assert!(mirror.acknowledge("b2", 99).is_err());
        assert!(mirror.pending.is_some());

        // Matching ack applies: cursor advances, queue drains, pending clears.
        assert_eq!(mirror.acknowledge("b2", 6), Ok(true));
        assert_eq!(mirror.after_seq, 6);
        assert!(mirror.pending.is_none());
        assert!(mirror.queue.is_empty());
    }
    #[test]
    fn local_sessions_tool_event_is_bounded_against_the_wire_not_its_byte_length() {
        // 20 KiB of CJK passes every byte bound, but the envelope charges 6 wire chars
        // per UTF-16 unit: unshrunk, this event can never fit alone and wedges the session.
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        let stdout = "漢字テスト".repeat(20 * 1024 / "漢字テスト".len() + 1);
        assert!(stdout.len() >= 20 * 1024);
        let row = json!({"type":"user","sessionId":"s","version":"2.1.269","uuid":"u1","timestamp":"2026-09-12T10:00:00.000Z",
            "toolUseResult":{"stdout":&stdout,"stderr":"","interrupted":false},
            "message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","content":&stdout,"is_error":false}]}});
        fs::write(&transcript, format!("{row}\n")).unwrap();
        let mut mirror = test_mirror(transcript);
        let batch = mirror
            .next_batch()
            .expect("a CJK tool result must not wedge the queue")
            .unwrap();
        let events = batch["events"].as_array().unwrap();
        assert_eq!(events.len(), 1, "a tool event is never split");
        assert!(ascii_wire_len(&events[0]).unwrap() <= 15_000);
        assert_eq!(events[0]["truncated"], true);
    }

    #[test]
    fn local_sessions_admissible_drops_ids_and_names_the_backend_would_reject() {
        // Upstream ids travel verbatim; a rejected batch tears the connection down and
        // replays the same head of queue forever.
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("transcript.jsonl");
        let row = json!({"type":"assistant","sessionId":"s","version":"2.1.269","uuid":"bad id","timestamp":"2026-09-12T10:00:00.000Z",
            "message":{"role":"assistant","content":[{"type":"text","text":"hello"},{"type":"tool_use","id":"toolu_1","name":"","input":{}}]}});
        fs::write(&transcript, format!("{row}\n")).unwrap();
        let mut mirror = test_mirror(transcript);
        let batch = mirror.next_batch().unwrap().unwrap();
        let events = batch["events"].as_array().unwrap();
        assert_eq!(events[0]["text"], "hello");
        assert!(
            events[0].get("itemId").is_none(),
            "an itemId with a space cannot match the backend's pattern"
        );
        assert_eq!(events[1]["tool"]["name"], "tool");
    }

    #[test]
    fn local_sessions_next_batch_marks_an_unsendable_event_instead_of_wedging() {
        let dir = tempfile::tempdir().unwrap();
        let mut mirror = test_mirror(dir.path().join("absent.jsonl"));
        // Nothing shrinks actor.userId, so this event can never fit alone.
        mirror.queue = vec![
            json!({"kind":"text","role":"user","text":"hi","truncated":false,
            "ts":"2026-09-12T10:00:00.000Z","itemId":"u1","actor":{"userId":"漢".repeat(4000)}}),
        ];
        let batch = mirror
            .next_batch()
            .expect("an unsendable event must not wedge the queue")
            .unwrap();
        let event = &batch["events"][0];
        assert_eq!(
            (&event["kind"], &event["role"]),
            (&json!("text"), &json!("system"))
        );
        assert!(event["text"]
            .as_str()
            .unwrap()
            .contains("could not mirror one event"));
        assert_eq!(event["truncated"], true);
        assert_eq!(event["itemId"], "u1");
        assert_eq!(event["ts"], "2026-09-12T10:00:00.000Z");
        assert!(ascii_wire_len(&batch).unwrap() <= 16384);
        assert_eq!(
            mirror.queue.len(),
            1,
            "the marker replaces the event in place"
        );
        mirror
            .acknowledge(batch["batchId"].as_str().unwrap(), 1)
            .unwrap();
        assert!(mirror.queue.is_empty());
    }

    #[test]
    fn local_sessions_helpers_bound_preview_and_redact() {
        assert_eq!(bounded("héllo", 3), ("hé".into(), true));
        assert_eq!(preview("a\nb\nc", 2), ("a\nb".into(), 3));
        let leaked = "token AKIAIOSFODNN7EXAMPLE and sk-abcdefghijklmnopqrstuvwxyz12 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc plain sk-short";
        let clean = redact(leaked);
        assert!(
            !clean.contains("AKIAIOSFODNN7EXAMPLE")
                && !clean.contains("sk-abcdefghijklmnopqrstuvwxyz12")
                && !clean.contains("eyJhbGci")
        );
        assert!(clean.contains("plain sk-short"));
        assert_eq!(
            redact("-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\nafter"),
            "[redacted]\nafter"
        );
        // A 20-line tool preview can cut the block before its END marker.
        assert_eq!(
            redact("x\n-----BEGIN RSA PRIVATE KEY-----\nMIIE\nMIIF"),
            "x\n[redacted]"
        );
        assert_eq!(redact("nothing to hide here"), "nothing to hide here");
        let (big, cut) = bound_json(json!({"stdout": "x".repeat(20_000), "code": 0}), 4096);
        assert!(serde_json::to_string(&big).unwrap().len() <= 4096 && big["code"] == 0);
        assert!(cut, "a shrunken payload reports itself truncated");
        assert_eq!(
            bound_json(json!({"stdout": "small", "code": 0}), 4096),
            (json!({"stdout": "small", "code": 0}), false)
        );
        assert_eq!(
            bound_json(json!(vec![1u8; 1000]), 64),
            (Value::Null, true),
            "nothing shrinkable left"
        );
    }

    #[test]
    fn local_sessions_event_builders_and_teammate_header_parse() {
        let e = event(
            "text",
            "assistant",
            "hi",
            Some("2026-09-12T10:00:00.000Z"),
            Some("m1"),
        );
        assert_eq!(
            e,
            json!({"kind":"text","role":"assistant","text":"hi","truncated":false,"ts":"2026-09-12T10:00:00.000Z","itemId":"m1"})
        );
        assert_eq!(
            lifecycle("turn_end", "", None, None)["lifecycle"],
            "turn_end"
        );
        let labelled = "[Lightbulb teammate contribution from @aden (Aden Kim) {\"roomId\":\"r\",\"requestId\":\"q\",\"actorUserId\":\"user_1\",\"actorHandle\":\"aden\"}]\n[Lightbulb workspace advisory: something changed.]\nCheck concurrency 50";
        let (provenance, body) = teammate_contribution(labelled).unwrap();
        assert_eq!(body, "Check concurrency 50");
        assert_eq!(actor_of(&provenance), Some(json!({"userId":"user_1"})));
        let legacy = "[Lightbulb teammate contribution {\"roomId\":\"r\",\"requestId\":\"q\",\"actor\":{\"kind\":\"agent\",\"id\":\"friday\"}}]\nhello";
        let (provenance, body) = teammate_contribution(legacy).unwrap();
        assert_eq!(
            (body.as_str(), actor_of(&provenance)),
            ("hello", Some(json!({"agentId":"friday"})))
        );
        let brace_in_name = "[Lightbulb teammate contribution from @aden (Aden {Kim}) {\"roomId\":\"r\",\"requestId\":\"q\",\"actorUserId\":\"user_a\"}]\nhello";
        let (provenance, body) = teammate_contribution(brace_in_name).unwrap();
        assert_eq!(
            (body.as_str(), actor_of(&provenance)),
            ("hello", Some(json!({"userId":"user_a"})))
        );
        assert!(teammate_contribution("ordinary prompt").is_none());
        assert!(teammate_contribution("[Lightbulb teammate contribution not-json]\nx").is_none());
    }

    #[test]
    fn local_sessions_command_header_names_the_sender_and_keeps_the_hash_stable() {
        let mut cmd = command();
        let unlabelled = serde_json::to_string(&cmd).unwrap();
        assert_eq!(
            cmd.header(),
            format!("[Lightbulb teammate contribution {}]\n", cmd.provenance())
        );
        cmd.label = Some(ActorLabel {
            kind: "human".into(),
            handle: Some("aden".into()),
            display_name: Some("Aden Kim".into()),
        });
        assert_eq!(
            serde_json::to_string(&cmd).unwrap(),
            unlabelled,
            "label never enters the idempotency hash"
        );
        let header = cmd.header();
        assert!(header.starts_with("[Lightbulb teammate contribution from @aden (Aden Kim) {"));
        assert_eq!(cmd.provenance()["actorHandle"], "aden");
        let (provenance, body) = teammate_contribution(&format!("{header}hello")).unwrap();
        assert_eq!(
            (provenance["actorUserId"].as_str(), body.as_str()),
            (Some("alice"), "hello")
        );
        cmd.label = Some(ActorLabel {
            kind: "human".into(),
            handle: Some("aden".into()),
            display_name: Some("Aden {Kim]\n".into()),
        });
        assert!(
            cmd.header()
                .starts_with("[Lightbulb teammate contribution from @aden (Aden Kim) {"),
            "delimiters never come from a label"
        );
        assert!(teammate_contribution(&format!("{}hello", cmd.header())).is_some());
        cmd.own = true;
        assert_eq!(cmd.header(), "[Sent from Lightbulb by you]\n");
        assert!(
            teammate_contribution(&format!("{}hello", cmd.header())).is_none(),
            "the owner's prompt is ordinary native input"
        );
    }

    #[test]
    fn local_sessions_subagents_mirror_into_the_parent_stream_only_once_the_relay_reads_them() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("s.jsonl");
        let row = |text: &str| json!({"type":"assistant","sessionId":"s","version":"2.1.269","uuid":"u",
            "message":{"role":"assistant","content":[{"type":"text","text":text}]}}).to_string();
        let notification = json!({"type":"user","sessionId":"s","version":"2.1.269","uuid":"n",
            "message":{"role":"user","content":"<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n<summary>Agent \"Survey\" finished</summary>\n</task-notification>"}}).to_string();
        fs::write(&transcript, format!("{}\n{notification}\n", row("parent"))).unwrap();
        let subs = dir.path().join("s/subagents");
        fs::create_dir_all(&subs).unwrap();
        fs::write(subs.join("agent-a1.jsonl"), format!("{}\n", row("child"))).unwrap();
        fs::write(subs.join("agent-a1.meta.json"), r#"{"agentType":"Explore","description":"Survey","toolUseId":"toolu_1"}"#).unwrap();
        fs::write(subs.join("agent-bad id.jsonl"), "").unwrap();
        let mut odd = json!({"kind":"lifecycle","role":"system","text":"","lifecycle":"subagent_end","subagent":"not opaque!"});
        admissible(&mut odd);
        assert!(odd.get("subagent").is_none(), "a harness id the relay would reject never travels");

        // An older relay: nothing tagged is read, and the parent's own tagged end is dropped.
        let mut old = test_mirror(transcript.clone());
        let batch = old.next_batch().unwrap().unwrap();
        let texts: Vec<_> = batch["events"].as_array().unwrap().iter().map(|e| e["text"].clone()).collect();
        assert_eq!(texts, vec![json!("parent")]);
        assert!(old.subagents.is_empty());

        let mut mirror = test_mirror(transcript.clone());
        mirror.subagents.relay = true;
        let batch = mirror.next_batch().unwrap().unwrap();
        let events = batch["events"].as_array().unwrap();
        let shape: Vec<_> = events.iter().map(|e| (e["subagent"].as_str(), e["lifecycle"].as_str(), e["text"].as_str().unwrap())).collect();
        assert_eq!(shape, vec![
            (None, None, "parent"),
            (Some("a1"), Some("subagent_end"), "Agent \"Survey\" finished"),
            (Some("a1"), Some("subagent_start"), "Survey"),
            (Some("a1"), None, "child"),
        ]);
        assert_eq!(events[1]["tool"]["result"]["background"], true);
        assert_eq!(events[2]["parentId"], "toolu_1");
        assert_eq!(events[2]["tool"]["input"]["subagentType"], "Explore");
        assert_eq!(mirror.subagents.known.keys().collect::<Vec<_>>(), vec!["a1"], "an id that is not opaque is never read");

        let mut downgraded = mirror.clone();
        downgraded.subagents.relay = false;
        let fallback = downgraded.next_batch().unwrap().unwrap();
        assert_eq!(fallback["events"].as_array().unwrap().len(), 1);
        assert_eq!(fallback["events"][0]["text"], "parent");
        assert!(fallback["events"][0].get("subagent").is_none());

        // New child output is read on its own cursor; a child that stops making sense is
        // closed on its own and never gaps or unverifies the parent.
        mirror.acknowledge(batch["batchId"].as_str().unwrap(), 4).unwrap();
        let mut child = fs::OpenOptions::new().append(true).open(subs.join("agent-a1.jsonl")).unwrap();
        writeln!(child, "{}", row("more")).unwrap();
        let batch = mirror.next_batch().unwrap().unwrap();
        assert_eq!(batch["events"][0]["text"], "more");
        assert_eq!(batch["events"][0]["subagent"], "a1");
        mirror.acknowledge(batch["batchId"].as_str().unwrap(), 5).unwrap();
        writeln!(child, "{}", json!({"type":"assistant","sessionId":"other","version":"2.1.269","message":{"role":"assistant","content":[]}})).unwrap();
        let mut parent = fs::OpenOptions::new().append(true).open(&transcript).unwrap();
        writeln!(parent, "{}", row("still here")).unwrap();
        let batch = mirror.next_batch().unwrap().unwrap();
        assert_eq!(batch["events"].as_array().unwrap().len(), 1);
        assert_eq!(batch["events"][0]["text"], "still here");
        assert!(mirror.subagents.known["a1"].closed);
        assert!(mirror.native.restriction.is_none());
    }

    #[test]
    fn local_sessions_a_subagent_names_and_ends_itself_from_its_own_files() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("s.jsonl");
        fs::write(&transcript, "").unwrap();
        let subs = dir.path().join("s/subagents");
        fs::create_dir_all(&subs).unwrap();
        let ask = json!({"type":"user","sessionId":"s","version":"2.1.269","uuid":"q","timestamp":"2026-09-25T10:00:00.000Z",
            "message":{"role":"user","content":"Find   every early return\nin jev.py and report back"}}).to_string();
        let answer = |stop: Value, id: &str| json!({"type":"assistant","sessionId":"s","version":"2.1.269","uuid":id,"timestamp":"2026-09-25T10:05:00.000Z",
            "message":{"id":id,"role":"assistant","stop_reason":stop,"usage":{"output_tokens":700},"content":[{"type":"text","text":"Three."}]}}).to_string();
        // No meta at all: the first ask names it. An answer with end_turn ends it at once.
        fs::write(subs.join("agent-a1.jsonl"), format!("{ask}\n{}\n", answer(json!("end_turn"), "m1"))).unwrap();
        // Claude often writes the last answer with no stop reason: quiet after it ends it.
        fs::write(subs.join("agent-a2.jsonl"), format!("{ask}\n{}\n", answer(Value::Null, "m2"))).unwrap();
        let mut mirror = test_mirror(transcript);
        mirror.subagents.relay = true;
        let batch = mirror.next_batch().unwrap().unwrap();
        let marks: Vec<_> = batch["events"].as_array().unwrap().iter()
            .filter(|e| e["lifecycle"].as_str().is_some_and(|l| l.starts_with("subagent")))
            .map(|e| (e["subagent"].as_str().unwrap().to_owned(), e["lifecycle"].as_str().unwrap().to_owned(), e["text"].as_str().unwrap().to_owned()))
            .collect();
        let named = "Find every early return in jev.py and report back";
        assert_eq!(marks, vec![
            ("a1".into(), "subagent_start".into(), "".into()),
            ("a2".into(), "subagent_start".into(), "".into()),
            ("a1".into(), "subagent_start".into(), named.into()),
            ("a1".into(), "subagent_end".into(), format!("Agent \"{named}\" finished")),
            ("a2".into(), "subagent_start".into(), named.into()),
        ]);
        let end = batch["events"].as_array().unwrap().iter().find(|e| e["lifecycle"] == "subagent_end").unwrap();
        assert_eq!(end["tool"]["result"], json!({"status":"completed","background":false,"tokens":700}));
        let n = batch["events"].as_array().unwrap().len() as u64;
        mirror.acknowledge(batch["batchId"].as_str().unwrap(), n).unwrap();
        assert!(mirror.next_batch().unwrap().is_none(), "not quiet long enough yet");
        let old = std::time::SystemTime::now() - std::time::Duration::from_secs(60);
        fs::File::options().write(true).open(subs.join("agent-a2.jsonl")).unwrap().set_modified(old).unwrap();
        let batch = mirror.next_batch().unwrap().unwrap();
        assert_eq!(batch["events"][0]["lifecycle"], "subagent_end");
        assert_eq!(batch["events"][0]["subagent"], "a2");
        assert_eq!(batch["events"][0]["ts"], "2026-09-25T10:05:00.000Z");
        mirror.acknowledge(batch["batchId"].as_str().unwrap(), n + 1).unwrap();
        assert!(mirror.next_batch().unwrap().is_none(), "one end, not one per tick");
    }
}
