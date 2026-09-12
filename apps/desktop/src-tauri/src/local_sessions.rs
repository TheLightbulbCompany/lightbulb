//! Opt-in native terminal sharing. Provider processes and credentials stay local.
mod claude;
mod codex;
pub mod enrollment;
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
pub fn normalize_public_message(
    provider: &str,
    session: &str,
    row: &Value,
) -> Result<Option<Value>> {
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
pub(crate) const HEADER_PREFIX: &str = "[Lightbulb teammate contribution";
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
        let Some(end) = rest[start..].find("-----END ") else {
            break;
        };
        let tail = &rest[start + end + 9..];
        let close = tail.find("-----").map(|i| i + 5).unwrap_or(tail.len());
        out.push_str(&rest[..start]);
        out.push_str("[redacted]");
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

/// Redact every string leaf, then shrink string leaves until the JSON fits `max` bytes; Null if it cannot.
pub(crate) fn bound_json(mut value: Value, max: usize) -> Value {
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
    while serde_json::to_string(&value)
        .map(|s| s.len() > max)
        .unwrap_or(true)
    {
        if per < 64 {
            return Value::Null;
        }
        per /= 2;
        walk(&mut value, &|s| {
            let (cut, truncated) = bounded(s, per);
            if truncated {
                *s = format!("{cut}…");
            }
        });
    }
    value
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
    let provenance: Value = serde_json::from_str(&header[header.find('{')?..]).ok()?;
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
    fn authenticate(&mut self, human: Option<&str>) {
        self.paused = human != Some(self.human_user_id.as_str());
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct AgentActor {
    kind: String,
    id: String,
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
        value
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
        let admit = ordered
            && cmd.expected_generation == generation
            && cmd.authorized_at(now)
            && cmd.created_at <= now
            && cmd.deliver_by.saturating_sub(cmd.created_at) <= 30
            && cmd.state == "dispatched"
            && cmd.kind == "send"
            && cmd.expected_turn_id.is_none()
            && cmd.command_seq > 0
            && cmd
                .text
                .as_ref()
                .is_some_and(|t| !t.trim().is_empty() && t.len() <= 8192);
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
        let mut reader = BufReader::new(file.take(2 * 1024 * 1024 + 1));
        let mut rows = vec![];
        let mut consumed = 0;
        while consumed < 65536 && rows.len() < 32 {
            let mut bytes = vec![];
            let n = reader
                .read_until(b'\n', &mut bytes)
                .map_err(|e| e.to_string())?;
            if n > 2 * 1024 * 1024 {
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

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
struct Process {
    pid: u32,
    birth_micros: u64,
    executable: PathBuf,
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

fn provider_program(home: &Path, name: &str) -> Option<PathBuf> {
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

async fn runtime_version(expected: &Process, installed: &Path, provider: &str) -> Result<String> {
    // Never execute an arbitrary path obtained from a registration or process row.
    // The mapped executable must be the resolved installed provider binary.
    if expected.executable != installed || process(expected.pid)? != *expected {
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
            if Path::new(value) == installed && inode == Some(before.ino()) {
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

fn reconcile_sessions(current: &mut Vec<NativeSession>, discovered: Vec<NativeSession>) {
    for session in current.iter_mut() {
        session.status = "offline".into();
    }
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
    let mut rest = text;
    let mut result = vec![];
    while !rest.is_empty() {
        let mut end = rest.len().min(1800);
        while !rest.is_char_boundary(end) {
            end -= 1;
        }
        let mut part = event.clone();
        part["text"] = json!(&rest[..end]);
        result.push(part);
        rest = &rest[end..];
    }
    result
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
    // RAM changes remain dirty across failed writes; this is never restored from disk.
    #[serde(skip)]
    dirty: bool,
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
    }
    fn advance(&mut self) -> bool {
        if self.registration.pending || self.pending.is_some() || !self.queue.is_empty() {
            return false;
        }
        let Some(native) = self.registration.successor.take() else {
            return false;
        };
        self.previous_generation = self.registration.acknowledged.clone();
        self.generation =
            fingerprint(&serde_json::to_string(&native.process).expect("process serialization"));
        self.native = native;
        true
    }
    fn next_batch(&mut self) -> Result<Option<Value>> {
        if let Some(pending) = &self.pending {
            return Ok(Some(pending.clone()));
        }
        if self.queue.is_empty() && self.native.restriction.as_deref() != Some("history-gap") {
            if self.native.status != "offline"
                && !process(self.native.process.pid).is_ok_and(|p| p == self.native.process)
            {
                return Err("runtime-unverified".into());
            }
            let Some(path) = &self.native.transcript else {
                return Ok(None);
            };
            let mut cursor = self.cursor.clone();
            let mut queue = Vec::new();
            for row in cursor.read(path)? {
                let event = normalize_public_message(
                    &self.native.provider,
                    &self.native.conversation,
                    &row,
                )?;
                if let Some(mut event) = event {
                    let text = event["text"].as_str().unwrap_or("");
                    if text.len() > 65536 {
                        let mut end = 65536;
                        while !text.is_char_boundary(end) {
                            end -= 1;
                        }
                        event["text"] = json!(&text[..end]);
                        event["truncated"] = json!(true);
                    }
                    queue.extend(split_text(&event));
                }
            }
            self.cursor = cursor;
            self.queue = queue;
        }
        let Some(session_id) = &self.session_id else {
            return Ok(None);
        };
        let Some(event) = self.queue.first() else {
            return Ok(None);
        };
        let batch = json!({"type":"local.session.events", "sessionId":session_id, "generation":self.generation,
            "batchId":uuid::Uuid::new_v4().to_string(), "afterSeq":self.after_seq, "events":[event]});
        // One bounded event per envelope, including escaping; no provider output
        // bypasses this final serialized-wire size check.
        if ascii_wire_len(&batch)? > 16384 {
            return Err("event envelope exceeds limit".into());
        }
        self.pending = Some(batch.clone());
        Ok(Some(batch))
    }
    fn acknowledge(&mut self, batch_id: &str, after_seq: u64) -> Result<()> {
        let pending = self
            .pending
            .as_ref()
            .ok_or("unexpected upload acknowledgment")?;
        if pending["batchId"] != batch_id || after_seq != self.after_seq + 1 {
            return Err("upload cursor conflict".into());
        }
        self.after_seq = after_seq;
        self.queue.remove(0);
        self.pending = None;
        Ok(())
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
        enrollment.authenticate(None);
        assert!(enrollment.enabled && enrollment.paused);
        enrollment.authenticate(Some("bob"));
        assert!(enrollment.paused);
        enrollment.authenticate(Some("alice"));
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
        assert_eq!(sessions[0].status, "offline");
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
            },
            generation: "g".into(),
            previous_generation: None,
            session_id: Some("server-session".into()),
            cursor: Cursor::default(),
            after_seq: 0,
            queue: vec![],
            pending: None,
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
        restored.native.status = "offline".into();
        restored.native.process.pid = u32::MAX;
        assert_eq!(
            restored.next_batch().unwrap().unwrap()["events"][0]["text"],
            "final before exit",
            "an enrolled transcript's anchored final records survive process exit"
        );
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
        let big = bound_json(json!({"stdout": "x".repeat(20_000), "code": 0}), 4096);
        assert!(serde_json::to_string(&big).unwrap().len() <= 4096 && big["code"] == 0);
        assert_eq!(
            bound_json(json!(vec![1u8; 1000]), 64),
            Value::Null,
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
        assert!(teammate_contribution("ordinary prompt").is_none());
        assert!(teammate_contribution("[Lightbulb teammate contribution not-json]\nx").is_none());
    }
}
