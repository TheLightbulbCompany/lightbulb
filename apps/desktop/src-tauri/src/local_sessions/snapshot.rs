//! Session pick-up, author side: snapshot a session a teammate may pick up.
//!
//! The relay loop hands this task the `pickup` list from an acknowledgement; it finds
//! each transcript itself and uploads what is new. It never writes a mirror file, never
//! ends, re-registers or quarantines a session, and its errors are logged and swallowed:
//! a failing session is simply left alone for 30 seconds.
use super::transport::relay_log;
use super::*;
use crate::session_pickup::{
    anonymous_git, git, git_run, normalize_remote, CODE_MAX, SIDECARS_MAX,
};
use std::collections::{BTreeMap, HashMap};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

const CHUNK: u64 = 32 * 1024 * 1024;
const UNTRACKED_MAX: u64 = 200 * 1024 * 1024;
const TRANSCRIPT_MAX: u64 = 1024 * 1024 * 1024;
const BACKOFF: Duration = Duration::from_secs(30);
/// An idle session's tree is still re-checked this often: a person can edit files
/// without the session doing anything.
const CODE_RECHECK: Duration = Duration::from_secs(60);

#[derive(Clone, Debug)]
pub(super) struct Wanted {
    session_id: String,
    conversation_id: String,
    busy: bool,
    ended: bool,
}

// One sender per running relay host. A global slot keeps the relay loop's own
// state untouched: it only offers, and a full or closed channel drops the list.
static WANTED: std::sync::Mutex<Option<mpsc::Sender<Vec<Wanted>>>> = std::sync::Mutex::new(None);

pub(super) fn channel() -> mpsc::Receiver<Vec<Wanted>> {
    let (tx, rx) = mpsc::channel(1);
    if let Ok(mut slot) = WANTED.lock() {
        *slot = Some(tx);
    }
    rx
}

/// Called from the relay loop with an acknowledgement's `pickup` key (absent on an
/// older backend). Tags each entry from the mirror holding that session id.
pub(super) fn offer(pickup: &Value, mirrors: &BTreeMap<String, Mirror>) {
    let wanted = tag(pickup, mirrors);
    if wanted.is_empty() {
        return;
    }
    if let Some(tx) = WANTED.lock().ok().and_then(|s| s.clone()) {
        let _ = tx.try_send(wanted);
    }
}

fn tag(pickup: &Value, mirrors: &BTreeMap<String, Mirror>) -> Vec<Wanted> {
    pickup
        .as_array()
        .into_iter()
        .flatten()
        .take(40)
        .filter(|e| e["provider"] == "claude")
        .filter_map(|e| {
            let sid = e["sessionId"]
                .as_str()
                .filter(|s| uuid::Uuid::parse_str(s).is_ok())?;
            let conversation = e["conversationId"]
                .as_str()
                .filter(|s| uuid::Uuid::parse_str(s).is_ok())?;
            let found = mirrors
                .iter()
                .find(|(_, m)| m.session_id.as_deref() == Some(sid));
            // A session that is not shared hands over nothing: no conversation, no changes.
            if found.is_some_and(|(key, m)| super::transport::stopped(key, m)) {
                return None;
            }
            let mirror = found.map(|(_, m)| m);
            Some(Wanted {
                session_id: sid.into(),
                conversation_id: conversation.into(),
                busy: mirror.is_some_and(|m| m.native.status == "busy"),
                // A reaped mirror is an ended session.
                ended: mirror.is_none_or(|m| m.ended_at.is_some()),
            })
        })
        .collect()
}

pub(crate) struct Author {
    pub dir: PathBuf,
    pub api: String,
    pub key_pem: String,
    pub device: String,
    pub owner: String,
}

/// Runs on its own task (see `enrollment.rs`), never inside the relay's `select!`, so no
/// amount of work here can delay a relay frame. Heavy synchronous stretches still go to
/// `spawn_blocking` so they do not hold a runtime worker either.
pub(super) async fn run(mut rx: mpsc::Receiver<Vec<Wanted>>, author: Author) {
    let mut resting = HashMap::<String, Instant>::new();
    let mut code_checked = HashMap::<String, Instant>::new();
    while let Some(list) = rx.recv().await {
        for wanted in list {
            if wanted.busy
                || resting
                    .get(&wanted.session_id)
                    .is_some_and(|at| at.elapsed() < BACKOFF)
            {
                continue;
            }
            if let Err(e) = snapshot(&author, &wanted, &mut code_checked).await {
                relay_log(&format!(
                    "{} pick-up snapshot failed: {e}",
                    &wanted.session_id[..8]
                ));
                resting.insert(wanted.session_id.clone(), Instant::now());
            }
        }
    }
}

async fn blocking<T: Send + 'static>(
    work: impl FnOnce() -> Result<T> + Send + 'static,
) -> Result<T> {
    tokio::task::spawn_blocking(work)
        .await
        .map_err(|_| "snapshot worker failed".to_string())?
}

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
struct Local {
    series: String,
    offset: u64,
    inode: u64,
    next_index: u64,
    code_key: Option<String>,
    sidecars_sha: Option<String>,
    ended: bool,
    /// The transcript's length when last looked at (the offset stops at the last newline).
    #[serde(default)]
    seen_len: u64,
    /// Listing signature of the sidecar files the stored `sidecars_sha` was built from.
    #[serde(default)]
    sidecars_sig: Option<String>,
}

impl Local {
    fn fresh(inode: u64) -> Self {
        Local {
            series: uuid::Uuid::new_v4().to_string(),
            inode,
            ..Default::default()
        }
    }
}

/// Nothing to do, decided from cheap facts only: the transcript's length (inode already
/// matched), the sidecar listing's signature, whether the code check is due, and `ended`.
fn quiet(
    fresh: bool,
    local: &Local,
    len: u64,
    signature: &str,
    code_due: bool,
    ended: bool,
) -> bool {
    !fresh
        && len == local.seen_len
        && local.sidecars_sig.as_deref() == Some(signature)
        && !code_due
        && local.ended == ended
}

/// Whether this pass moves what the backend holds. A committed bundle that is now too
/// large, or whose repository is gone, counts: the commit's `codeSkipped` is what stops
/// the old bundle being offered.
fn moved(local: &Local, parts: usize, code: &CodeOutcome, side: &Side) -> bool {
    parts > 0
        || !matches!(side, Side::Same)
        || matches!(code, CodeOutcome::Captured(_))
        || (matches!(code, CodeOutcome::TooLarge | CodeOutcome::None) && local.code_key.is_some())
}

/// What the commit tells the backend to drop. A null alone keeps what the series holds.
fn skipped(code: &CodeOutcome, side: &Side) -> serde_json::Map<String, Value> {
    let mut out = serde_json::Map::new();
    match code {
        CodeOutcome::TooLarge => {
            out.insert("codeSkipped".into(), json!("too-large"));
        }
        CodeOutcome::None => {
            out.insert("codeSkipped".into(), json!("none"));
        }
        _ => {}
    }
    if matches!(side, Side::Gone) {
        out.insert("sidecarsSkipped".into(), json!("none"));
    }
    out
}

#[derive(Debug, PartialEq)]
enum Side {
    Same,
    New(Vec<u8>, String),
    /// Every sidecar file is gone but a committed archive still stands.
    Gone,
}

/// What the rebuilt sidecar archive (`None`: no files) means against what was committed.
fn side_change(local: &Local, built: Option<(Vec<u8>, String)>) -> Side {
    match built {
        Some((_, digest)) if local.sidecars_sha.as_deref() == Some(digest.as_str()) => Side::Same,
        Some((bytes, digest)) => Side::New(bytes, digest),
        None if local.sidecars_sha.is_some() => Side::Gone,
        None => Side::Same,
    }
}

fn config_dir() -> Option<PathBuf> {
    std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".claude")))
}

pub(super) fn find_transcript(config: &Path, conversation: &str) -> Option<PathBuf> {
    fs::read_dir(config.join("projects"))
        .ok()?
        .flatten()
        .map(|e| e.path().join(format!("{conversation}.jsonl")))
        .filter(|p| p.is_file())
        .max_by_key(|p| fs::metadata(p).and_then(|m| m.modified()).ok())
}

/// `cwd` and `version` from the newest rows that carry them.
pub(super) fn cwd_and_version(path: &Path) -> Result<(Option<PathBuf>, Option<String>)> {
    let mut file = fs::File::open(path).map_err(|_| "transcript unreadable")?;
    let len = file.metadata().map_err(|_| "transcript unreadable")?.len();
    file.seek(SeekFrom::Start(len.saturating_sub(1024 * 1024)))
        .map_err(|_| "transcript unreadable")?;
    let mut tail = vec![];
    file.read_to_end(&mut tail)
        .map_err(|_| "transcript unreadable")?;
    let (mut cwd, mut version) = (None, None);
    for line in tail.split(|b| *b == b'\n').rev() {
        let Ok(row) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        cwd = cwd.or_else(|| row["cwd"].as_str().map(PathBuf::from));
        version = version.or_else(|| row["version"].as_str().map(str::to_owned));
        if cwd.is_some() && version.is_some() {
            break;
        }
    }
    Ok((cwd, version))
}

/// Committed ranges and compressed metadata only; never retain the whole transcript.
/// (from, to, gzip length, sha256 of the gzip bytes).
type Part = (u64, u64, usize, String);

fn gzip_part(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut gz = flate2::write::GzEncoder::new(vec![], flate2::Compression::default());
    gz.write_all(bytes).map_err(|e| e.to_string())?;
    gz.finish().map_err(|e| e.to_string())
}

fn transcript_part(path: &Path, from: u64, to: u64) -> Result<Vec<u8>> {
    let size = to
        .checked_sub(from)
        .filter(|size| *size > 0 && *size <= CHUNK)
        .ok_or("invalid transcript range")?;
    let mut file = fs::File::open(path).map_err(|_| "transcript unreadable")?;
    file.seek(SeekFrom::Start(from))
        .map_err(|_| "transcript unreadable")?;
    let mut bytes = vec![0; size as usize];
    file.read_exact(&mut bytes)
        .map_err(|_| "transcript changed")?;
    gzip_part(&bytes)
}

fn transcript_parts(path: &Path, offset: u64, len: u64) -> Result<Vec<Part>> {
    let mut file = fs::File::open(path).map_err(|_| "transcript unreadable")?;
    // Find the last complete line without allocating a possibly 1 GiB delta.
    let mut end = len;
    let mut complete = false;
    while end > offset {
        let from = end.saturating_sub(64 * 1024).max(offset);
        file.seek(SeekFrom::Start(from))
            .map_err(|_| "transcript unreadable")?;
        let mut bytes = vec![0; (end - from) as usize];
        file.read_exact(&mut bytes)
            .map_err(|_| "transcript changed")?;
        if let Some(index) = bytes.iter().rposition(|b| *b == b'\n') {
            end = from + index as u64 + 1;
            complete = true;
            break;
        }
        end = from;
    }
    if !complete {
        return Ok(vec![]);
    }
    let mut parts = vec![];
    let mut start = offset;
    while start < end {
        file.seek(SeekFrom::Start(start))
            .map_err(|_| "transcript unreadable")?;
        let mut bytes = vec![0; (end - start).min(CHUNK) as usize];
        file.read_exact(&mut bytes)
            .map_err(|_| "transcript changed")?;
        let cut = bytes
            .iter()
            .rposition(|b| *b == b'\n')
            .map_or(bytes.len(), |i| i + 1);
        let gz = gzip_part(&bytes[..cut])?;
        parts.push((start, start + cut as u64, gz.len(), sha(&gz)));
        start += cut as u64;
    }
    Ok(parts)
}

/// The regular files under `<transcript dir>/<conversation>/{subagents,tool-results}`,
/// sorted, as (path relative to that dir, full path, metadata).
fn sidecar_files(
    transcript: &Path,
    conversation: &str,
) -> Result<Vec<(PathBuf, PathBuf, fs::Metadata)>> {
    let base = transcript.with_file_name(conversation);
    let mut files = vec![];
    for name in ["subagents", "tool-results"] {
        let root = base.join(name);
        if !root.is_dir() {
            continue;
        }
        for entry in walkdir::WalkDir::new(&root)
            .sort_by_file_name()
            .follow_links(false)
        {
            let entry = entry.map_err(|_| "sidecars unreadable")?;
            if !entry.file_type().is_file() {
                continue;
            }
            let relative = entry
                .path()
                .strip_prefix(&base)
                .map_err(|_| "sidecars unreadable")?;
            let meta = entry.metadata().map_err(|_| "sidecars unreadable")?;
            files.push((relative.to_path_buf(), entry.path().to_path_buf(), meta));
        }
    }
    Ok(files)
}

/// A cheap stand-in for the archive: path, size and mtime of every file. The archive is
/// rebuilt only when this moves.
fn sidecar_signature(transcript: &Path, conversation: &str) -> Result<String> {
    let mut hash = Sha256::new();
    for (relative, _, meta) in sidecar_files(transcript, conversation)? {
        hash.update(relative.as_os_str().as_bytes());
        hash.update(format!(
            "\0{}\0{}.{}\0",
            meta.len(),
            meta.mtime(),
            meta.mtime_nsec()
        ));
    }
    Ok(hex::encode(hash.finalize()))
}

/// The same files as a deterministic tar.gz (sorted, zeroed mtimes) and its sha256.
/// `None` when there are none, or when they exceed `SIDECARS_MAX`: oversized sidecars are
/// left out like deleted ones, so the transcript and code still refresh.
fn sidecars(transcript: &Path, conversation: &str) -> Result<Option<(Vec<u8>, String)>> {
    sidecars_within(transcript, conversation, SIDECARS_MAX)
}

fn sidecars_within(
    transcript: &Path,
    conversation: &str,
    max: u64,
) -> Result<Option<(Vec<u8>, String)>> {
    let files = sidecar_files(transcript, conversation)?;
    if files.is_empty() || files.iter().map(|(_, _, m)| m.len()).sum::<u64>() > max {
        return Ok(None);
    }
    let mut tar = tar::Builder::new(flate2::write::GzEncoder::new(
        vec![],
        flate2::Compression::default(),
    ));
    let mut total = 0u64;
    for (relative, path, _) in files {
        let data = fs::read(&path).map_err(|_| "sidecars unreadable")?;
        total += data.len() as u64;
        // A file grew since the listing.
        if total > max {
            return Ok(None);
        }
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o600);
        header.set_mtime(0);
        header.set_entry_type(tar::EntryType::Regular);
        tar.append_data(&mut header, &relative, data.as_slice())
            .map_err(|_| "sidecars unreadable")?;
    }
    let bytes = tar
        .into_inner()
        .and_then(|gz| gz.finish())
        .map_err(|_| "sidecars unreadable")?;
    // Tar and gzip overhead can push incompressible files past the upload cap.
    if bytes.len() as u64 > max {
        return Ok(None);
    }
    let digest = sha(&bytes);
    Ok(Some((bytes, digest)))
}

pub(crate) struct Code {
    pub bytes: Vec<u8>,
    pub sha: String,
    pub remote: String,
    pub branch: Option<String>,
    pub head: String,
    pub snapshot: String,
    pub key: String,
}

pub(crate) enum CodeOutcome {
    /// Not a git work tree with an `origin`, or no commit yet.
    None,
    TooLarge,
    Unchanged,
    Captured(Code),
}

/// Bundle the author's whole working state -- unpushed commits, uncommitted edits,
/// deletions, untracked files -- as one commit on top of their real HEAD, through a
/// COPY of their index so their index and working tree are untouched. `work` is a
/// private scratch directory.
pub(crate) async fn capture_code(
    top: &Path,
    sid: &str,
    work: &Path,
    known: Option<&str>,
) -> Result<CodeOutcome> {
    let Some(remote) = git(top, &["remote", "get-url", "origin"], None)
        .await
        .ok()
        .and_then(|o| normalize_remote(&o))
    else {
        return Ok(CodeOutcome::None);
    };
    let Ok(head) = git(top, &["rev-parse", "--verify", "HEAD"], None).await else {
        return Ok(CodeOutcome::None);
    };
    let untracked = git_run(
        top,
        &["ls-files", "--others", "--exclude-standard", "-z"],
        None,
        Duration::from_secs(120),
    )
    .await?;
    if !untracked.0 {
        return Err("git ls-files failed".into());
    }
    let size: u64 = untracked
        .1
        .split(|b| *b == 0)
        .filter(|p| !p.is_empty())
        .filter_map(|p| fs::symlink_metadata(top.join(std::ffi::OsStr::from_bytes(p))).ok())
        .map(|m| m.len())
        .sum();
    if size > UNTRACKED_MAX {
        return Ok(CodeOutcome::TooLarge);
    }
    let index = work.join("index");
    let real = top.join(git(top, &["rev-parse", "--git-path", "index"], None).await?);
    let copy = index.clone();
    blocking(move || {
        if real.is_file() {
            fs::copy(&real, &copy).map_err(|_| "index copy failed")?;
        }
        Ok(())
    })
    .await?;
    git(top, &["add", "-A"], Some(&index)).await?;
    let tree = git(top, &["write-tree"], Some(&index)).await?;
    let key = format!("{head}:{tree}");
    if known == Some(key.as_str()) {
        return Ok(CodeOutcome::Unchanged);
    }
    let snapshot = git(
        top,
        &[
            "-c",
            "user.name=Lightbulb",
            "-c",
            "user.email=pickup@lightbulb.invalid",
            "commit-tree",
            "--no-gpg-sign",
            &tree,
            "-p",
            &head,
            "-m",
            "lightbulb pick-up snapshot",
        ],
        None,
    )
    .await?;
    let reference = format!("refs/lightbulb/snap/{sid}");
    // A crash between update-ref and its deletion would otherwise leave one forever.
    let _ = git(top, &["update-ref", "-d", &reference], None).await;
    git(top, &["update-ref", &reference, &snapshot], None).await?;
    let bundle = work.join("snap.bundle");
    let made = git(
        top,
        &[
            "bundle",
            "create",
            &bundle.to_string_lossy(),
            &reference,
            "--not",
            "--remotes=origin",
        ],
        None,
    )
    .await;
    let _ = git(top, &["update-ref", "-d", &reference], None).await;
    // Size before reading: a bundle of a whole history can be gigabytes.
    let file = bundle.clone();
    let read = match made {
        Ok(_) => {
            blocking(move || {
                let len = fs::metadata(&file).map_err(|_| "bundle unreadable")?.len();
                if len > CODE_MAX {
                    return Ok(None);
                }
                let bytes = fs::read(&file).map_err(|_| "bundle unreadable")?;
                let digest = sha(&bytes);
                Ok(Some((bytes, digest)))
            })
            .await
        }
        Err(e) => Err(e),
    };
    let _ = fs::remove_file(&bundle);
    let Some((bytes, digest)) = read? else {
        return Ok(CodeOutcome::TooLarge);
    };
    let branch = git(top, &["symbolic-ref", "--short", "-q", "HEAD"], None)
        .await
        .ok();
    Ok(CodeOutcome::Captured(Code {
        bytes,
        sha: digest,
        remote,
        branch,
        head,
        snapshot,
        key,
    }))
}

/// Anonymous `ls-remote` answers, per normalized remote, for an hour (spec §3.3).
static PUBLIC: std::sync::LazyLock<std::sync::Mutex<HashMap<String, (Instant, bool)>>> =
    std::sync::LazyLock::new(Default::default);
const PUBLIC_FOR: Duration = Duration::from_secs(3600);

fn remember_public(remote: &str, public: bool) {
    PUBLIC
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .insert(remote.to_string(), (Instant::now(), public));
}

fn known_public(remote: &str) -> Option<bool> {
    let cache = PUBLIC.lock().unwrap_or_else(|e| e.into_inner());
    cache
        .get(remote)
        .filter(|(at, _)| at.elapsed() < PUBLIC_FOR)
        .map(|(_, public)| *public)
}

/// The anonymous probe: exit 0 means anyone can read the repository. Not `git_run`: that keeps
/// the user's global config on purpose, and a global `url.<ssh>.insteadOf https://` would make
/// this an SSH call with their keys.
fn probe_command(remote: &str) -> tokio::process::Command {
    anonymous_git(
        &std::env::temp_dir(),
        &["ls-remote", &format!("https://{remote}.git"), "HEAD"],
    )
}

async fn public(remote: &str) -> bool {
    if let Some(known) = known_public(remote) {
        return known;
    }
    let answer = tokio::time::timeout(Duration::from_secs(15), probe_command(remote).status())
        .await
        .is_ok_and(|status| status.is_ok_and(|s| s.success()));
    remember_public(remote, answer);
    answer
}

/// `{remote, branch, head, pushed, public, folder}` for the commit body, or null when this is not
/// a work tree with an `origin` and a commit. Sent on every commit, bundle or not (spec §3.3).
/// `folder` is the main checkout's folder name: the server hides it from outsiders when `public` is false.
pub(crate) async fn repo_info(top: &Path) -> Value {
    let Some(remote) = git(top, &["remote", "get-url", "origin"], None)
        .await
        .ok()
        .and_then(|o| normalize_remote(&o))
    else {
        return Value::Null;
    };
    // The backend's bounds (`RepoInfo`): a longer value would 422 every commit.
    if remote.chars().count() > 256 {
        return Value::Null;
    }
    let Ok(head) = git(top, &["rev-parse", "--verify", "HEAD"], None).await else {
        return Value::Null;
    };
    let pushed = git(
        top,
        &[
            "for-each-ref",
            "--count=1",
            "--contains",
            &head,
            "refs/remotes/origin",
        ],
        None,
    )
    .await
    .is_ok_and(|out| !out.is_empty());
    let branch = git(top, &["symbolic-ref", "--short", "-q", "HEAD"], None)
        .await
        .ok()
        .filter(|b| b.chars().count() <= 256);
    // The main checkout's folder (the git common dir's parent), not a linked worktree's: it is the
    // name every path in the transcript shows, usually the repository's own name.
    let main = git(
        top,
        &["rev-parse", "--path-format=absolute", "--git-common-dir"],
        None,
    )
    .await
    .ok()
    .and_then(|dir| PathBuf::from(dir).parent().map(Path::to_path_buf));
    let folder = main
        .as_deref()
        .unwrap_or(top)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .filter(|f| f.chars().count() <= 255);
    json!({"remote": remote, "branch": branch, "head": head, "pushed": pushed,
        "public": public(&remote).await, "folder": folder})
}

fn sha(bytes: &[u8]) -> String {
    hex::encode(Sha256::digest(bytes))
}

async fn post(
    author: &Author,
    client: &reqwest::Client,
    sid: &str,
    action: &str,
    body: &Value,
) -> Result<(u16, Value)> {
    let token = crate::device_token::mint_device_token(
        &author.key_pem,
        &author.device,
        &author.owner,
        crate::device_token::DEVICE_TOKEN_LIFETIME_SECS,
    )?;
    let url = format!(
        "{}/api/v1/desktop/session-pickup/{sid}/{action}",
        author.api.trim_end_matches('/')
    );
    let response = client
        .post(url)
        .bearer_auth(token)
        .json(body)
        .send()
        .await
        .map_err(|_| format!("{action} request failed"))?;
    let status = response.status().as_u16();
    Ok((status, response.json().await.unwrap_or(Value::Null)))
}

async fn snapshot(
    author: &Author,
    wanted: &Wanted,
    code_checked: &mut HashMap<String, Instant>,
) -> Result<()> {
    let client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    // A 409 means the backend's series and ours disagree: start a new one, once.
    for attempt in 0..2 {
        match snapshot_once(author, &client, wanted, attempt > 0, code_checked).await? {
            Some(409) if attempt == 0 => continue,
            Some(status) => return Err(format!("commit returned {status}")),
            None => return Ok(()),
        }
    }
    Ok(())
}

/// One pass. `Ok(None)` is done (or nothing to do); `Ok(Some(status))` a refused commit.
async fn snapshot_once(
    author: &Author,
    client: &reqwest::Client,
    wanted: &Wanted,
    restart: bool,
    code_checked: &mut HashMap<String, Instant>,
) -> Result<Option<u16>> {
    let sid = &wanted.session_id;
    let conversation = wanted.conversation_id.clone();
    let Some(transcript) = config_dir().and_then(|c| find_transcript(&c, &conversation)) else {
        // Claude writes the transcript at the first turn. Before it, there is nothing to send yet:
        // "missing" is final (the backend stops asking), so only an ended session says it.
        if !wanted.ended {
            return Ok(None);
        }
        return unavailable(author, client, sid, "transcript-missing").await;
    };
    let meta = fs::metadata(&transcript).map_err(|_| "transcript unreadable")?;
    if meta.len() > TRANSCRIPT_MAX {
        return unavailable(author, client, sid, "too-large").await;
    }
    let states = author.dir.join("snapshots");
    private_dir(&states)?;
    let state_path = states.join(format!("{sid}.json"));
    let previous: Option<Local> = private_read(&state_path).ok();
    let mut local = match &previous {
        Some(p) if !restart && p.inode == meta.ino() && meta.len() >= p.offset => p.clone(),
        _ => Local::fresh(meta.ino()),
    };
    // No state, or a new series: everything is sent.
    let fresh = previous.as_ref() != Some(&local);
    let (t, c) = (transcript.clone(), conversation.clone());
    let signature = blocking(move || sidecar_signature(&t, &c)).await?;
    let code_due = fresh
        || meta.len() != local.seen_len
        || code_checked
            .get(sid)
            .is_none_or(|at| at.elapsed() >= CODE_RECHECK);
    if quiet(
        fresh,
        &local,
        meta.len(),
        &signature,
        code_due,
        wanted.ended,
    ) {
        return Ok(None);
    }

    let t = transcript.clone();
    let (cwd, version) = blocking(move || cwd_and_version(&t)).await?;
    let Some(version) = version else {
        return unavailable(author, client, sid, "unsupported-version").await;
    };
    let parts = if fresh || meta.len() != local.seen_len {
        let (t, offset, len) = (transcript.clone(), local.offset, meta.len());
        blocking(move || transcript_parts(&t, offset, len)).await?
    } else {
        vec![]
    };

    let repo = match &cwd {
        Some(cwd) => git(cwd, &["rev-parse", "--show-toplevel"], None)
            .await
            .ok()
            .map(PathBuf::from),
        None => None,
    };
    let code = match &repo {
        Some(top) if code_due => {
            code_checked.insert(sid.clone(), Instant::now());
            let work = states.join(format!("tmp-{}", uuid::Uuid::new_v4()));
            private_dir(&work)?;
            let code = capture_code(top, sid, &work, local.code_key.as_deref()).await;
            let _ = fs::remove_dir_all(&work);
            code?
        }
        Some(_) => CodeOutcome::Unchanged,
        None => CodeOutcome::None,
    };
    let side = if local.sidecars_sig.as_deref() != Some(&signature) {
        let (t, c) = (transcript.clone(), conversation.clone());
        side_change(&local, blocking(move || sidecars(&t, &c)).await?)
    } else {
        Side::Same
    };
    local.seen_len = meta.len();
    local.sidecars_sig = Some(signature);

    let changed = moved(&local, parts.len(), &code, &side);
    if !fresh && !changed && local.ended == wanted.ended {
        // Only the cheap facts moved (a partial line, a touched but identical file):
        // remember them so the next pass returns early. Nothing sent is recorded here.
        if previous.as_ref() != Some(&local) {
            private_write(&state_path, &local)?;
        }
        return Ok(None);
    }

    let mut uploads: Vec<(Value, Option<&[u8]>)> = vec![];
    for (index, (_, _, size, digest)) in (local.next_index..).zip(&parts) {
        uploads.push((
            json!({"kind":"transcript","index":index,"bytes":size,"sha256":digest}),
            None,
        ));
    }
    if let CodeOutcome::Captured(c) = &code {
        uploads.push((
            json!({"kind":"code","bytes":c.bytes.len(),"sha256":c.sha}),
            Some(c.bytes.as_slice()),
        ));
    }
    if let Side::New(bytes, digest) = &side {
        uploads.push((
            json!({"kind":"sidecars","bytes":bytes.len(),"sha256":digest}),
            Some(bytes.as_slice()),
        ));
    }
    if !uploads.is_empty() {
        let body = json!({"series": local.series, "parts": uploads.iter().map(|(p, _)| p.clone()).collect::<Vec<_>>()});
        let (status, answer) = post(author, client, sid, "uploads", &body).await?;
        if status == 409 {
            return Ok(Some(409));
        }
        if !(200..300).contains(&status) {
            return Err(format!("uploads returned {status}"));
        }
        let urls = answer["uploads"]
            .as_array()
            .ok_or("uploads answer malformed")?;
        for (part, bytes) in &uploads {
            let url = urls
                .iter()
                .find(|u| {
                    u["kind"] == part["kind"]
                        && (part["kind"] != "transcript" || u["index"] == part["index"])
                })
                .and_then(|u| u["url"].as_str())
                .filter(|u| crate::session_pickup::allowed_url(u))
                .ok_or("upload url missing or refused")?;
            let bytes = match bytes {
                Some(bytes) => bytes.to_vec(),
                None => {
                    let index = part["index"].as_u64().ok_or("transcript index missing")?;
                    let range = index
                        .checked_sub(local.next_index)
                        .and_then(|index| parts.get(index as usize))
                        .ok_or("transcript range missing")?;
                    let (from, to, size, digest) = range;
                    let (path, from, to) = (transcript.clone(), *from, *to);
                    // ponytail: recompress one bounded range for upload instead of retaining
                    // every gzip; spool parts only if this extra compression becomes costly.
                    let gz = blocking(move || transcript_part(&path, from, to)).await?;
                    if gz.len() != *size || sha(&gz) != *digest {
                        return Err("transcript changed during snapshot".into());
                    }
                    gz
                }
            };
            let put = client
                .put(url)
                .body(bytes)
                .send()
                .await
                .map_err(|_| "upload failed")?;
            if !put.status().is_success() {
                return Err(format!("upload returned {}", put.status().as_u16()));
            }
        }
    }

    let top = repo.as_deref();
    let cwd_relative = match (cwd.as_deref().and_then(|c| fs::canonicalize(c).ok()), top) {
        (Some(c), Some(t)) => c
            .strip_prefix(t)
            .map(|r| r.to_string_lossy().into_owned())
            .unwrap_or_default(),
        _ => String::new(),
    };
    let next_index = local.next_index + parts.len() as u64;
    let offset = parts.last().map_or(local.offset, |p| p.1);
    let repo_info = match top {
        Some(t) => repo_info(t).await,
        None => Value::Null,
    };
    let mut body = json!({
        "series": local.series,
        "conversationId": conversation,
        "claudeVersion": version,
        "cwdName": cwd.as_deref().and_then(|c| c.file_name()).map(|n| n.to_string_lossy().into_owned()),
        "cwdRelative": cwd_relative,
        "ended": wanted.ended,
        "transcript": {
            "parts": (local.next_index..).zip(&parts).map(|(index, (from, to, size, digest))| json!({
                "index": index, "bytes": size, "sha256": digest, "from": from, "to": to,
            })).collect::<Vec<_>>(),
            "totalBytes": offset,
        },
        "code": match &code {
            CodeOutcome::Captured(c) => json!({"bytes": c.bytes.len(), "sha256": c.sha,
                "remote": c.remote, "branch": c.branch, "head": c.head, "snapshot": c.snapshot}),
            _ => Value::Null,
        },
        "sidecars": match &side {
            Side::New(bytes, digest) => json!({"bytes": bytes.len(), "sha256": digest}),
            _ => Value::Null,
        },
           "repo": repo_info,
    });
    if let Some(fields) = body.as_object_mut() {
        fields.extend(skipped(&code, &side));
    }
    let (status, _) = post(author, client, sid, "commit", &body).await?;
    if status == 409 {
        return Ok(Some(409));
    }
    if !(200..300).contains(&status) {
        return Ok(Some(status));
    }
    // Only a committed snapshot moves local state forward.
    local.offset = offset;
    local.next_index = next_index;
    local.ended = wanted.ended;
    match &code {
        CodeOutcome::Captured(c) => local.code_key = Some(c.key.clone()),
        // `codeSkipped` dropped the committed bundle.
        CodeOutcome::TooLarge | CodeOutcome::None => local.code_key = None,
        CodeOutcome::Unchanged => {}
    }
    match &side {
        Side::New(_, digest) => local.sidecars_sha = Some(digest.clone()),
        // `sidecarsSkipped` dropped the committed archive.
        Side::Gone => local.sidecars_sha = None,
        Side::Same => {}
    }
    private_write(&state_path, &local)?;
    relay_log(&format!(
        "{} pick-up snapshot committed: {} transcript bytes, code {}",
        &sid[..8],
        offset,
        match code {
            CodeOutcome::Captured(c) => format!("{} bytes", c.bytes.len()),
            CodeOutcome::TooLarge => "too-large".into(),
            CodeOutcome::Unchanged => "unchanged".into(),
            CodeOutcome::None => "none".into(),
        }
    ));
    Ok(None)
}

async fn unavailable(
    author: &Author,
    client: &reqwest::Client,
    sid: &str,
    reason: &str,
) -> Result<Option<u16>> {
    let (status, _) = post(
        author,
        client,
        sid,
        "unavailable",
        &json!({"reason": reason}),
    )
    .await?;
    if !(200..300).contains(&status) {
        return Err(format!("unavailable returned {status}"));
    }
    relay_log(&format!("{} pick-up unavailable: {reason}", &sid[..8]));
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn repo_info_reports_remote_branch_head_pushed_and_cached_public() {
        let dir = tempfile::tempdir().unwrap();
        let top = dir.path();
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(top)
                .status()
                .unwrap()
                .success())
        };
        g(&["init", "-q", "-b", "main"]);
        g(&[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "one",
        ]);
        g(&["remote", "add", "origin", "https://github.com/o/r.git"]);
        remember_public("github.com/o/r", true); // no network in tests
        let info = repo_info(top).await;
        assert_eq!(info["remote"], "github.com/o/r");
        assert_eq!(info["branch"], "main");
        assert_eq!(info["pushed"], false);
        assert_eq!(info["public"], true);
        assert_eq!(
            info["folder"],
            top.file_name().unwrap().to_string_lossy().as_ref()
        );
        g(&["update-ref", "refs/remotes/origin/main", "HEAD"]);
        assert_eq!(repo_info(top).await["pushed"], true);
        // A linked worktree reports the main checkout's folder, the name its paths show.
        let wt = top.join(".claude").join("worktrees").join("feature-x");
        g(&[
            "worktree",
            "add",
            "-q",
            "-b",
            "feature-x",
            wt.to_str().unwrap(),
        ]);
        assert_eq!(
            repo_info(&wt).await["folder"],
            top.file_name().unwrap().to_string_lossy().as_ref()
        );
    }

    #[tokio::test]
    async fn repo_info_drops_a_branch_over_the_backends_limit() {
        let dir = tempfile::tempdir().unwrap();
        let top = dir.path();
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("git")
                .args(args)
                .current_dir(top)
                .status()
                .unwrap()
                .success())
        };
        let long = format!("{}/{}", "x".repeat(200), "y".repeat(100));
        g(&["init", "-q", "-b", &long]);
        g(&[
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "one",
        ]);
        g(&["remote", "add", "origin", "https://github.com/o/long.git"]);
        remember_public("github.com/o/long", false);
        let info = repo_info(top).await;
        assert_eq!(info["remote"], "github.com/o/long");
        assert!(info["branch"].is_null());
    }

    #[tokio::test]
    async fn repo_info_is_null_without_origin() {
        let dir = tempfile::tempdir().unwrap();
        std::process::Command::new("git")
            .args(["init", "-q"])
            .current_dir(dir.path())
            .status()
            .unwrap();
        assert!(repo_info(dir.path()).await.is_null());
    }

    #[test]
    fn the_public_probe_ignores_the_users_git_config() {
        // A global `url.<ssh>.insteadOf https://` would turn the anonymous probe into an SSH call
        // with the user's keys, and a private repo would read as public.
        let cmd = probe_command("github.com/o/r");
        let std = cmd.as_std();
        let env: std::collections::HashMap<_, _> = std.get_envs().collect();
        assert_eq!(
            env.get(std::ffi::OsStr::new("GIT_CONFIG_GLOBAL"))
                .copied()
                .flatten(),
            Some(std::ffi::OsStr::new("/dev/null"))
        );
        assert_eq!(
            env.get(std::ffi::OsStr::new("GIT_CONFIG_NOSYSTEM"))
                .copied()
                .flatten(),
            Some(std::ffi::OsStr::new("1"))
        );
        assert_eq!(
            env.get(std::ffi::OsStr::new("GIT_ASKPASS"))
                .copied()
                .flatten(),
            Some(std::ffi::OsStr::new("/usr/bin/false"))
        );
        // Environment that would put config back, or turn TLS checks off, is removed.
        for key in [
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
            "GIT_SSL_NO_VERIFY",
            "GIT_PROXY_COMMAND",
        ] {
            assert_eq!(env.get(std::ffi::OsStr::new(key)), Some(&None), "{key}");
        }
        let args: Vec<_> = std
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            args,
            [
                "-c",
                "credential.helper=",
                "ls-remote",
                "https://github.com/o/r.git",
                "HEAD"
            ]
        );
    }

    #[test]
    fn snapshot_transcript_delta_is_complete_lines_only() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        fs::write(&path, "{\"a\":1}\n{\"b\":2}\n{\"partial\"").unwrap();
        let len = fs::metadata(&path).unwrap().len();
        let parts = transcript_parts(&path, 0, len).unwrap();
        assert_eq!(parts.len(), 1);
        let (from, to, size, digest) = &parts[0];
        let gz = transcript_part(&path, *from, *to).unwrap();
        assert_eq!(*size, gz.len());
        assert_eq!(digest, &sha(&gz));
        assert_eq!((*from, *to), (0, 16));
        let mut raw = String::new();
        flate2::read::GzDecoder::new(gz.as_slice())
            .read_to_string(&mut raw)
            .unwrap();
        assert_eq!(raw, "{\"a\":1}\n{\"b\":2}\n");
        assert!(transcript_parts(&path, 16, len).unwrap().is_empty());
        assert!(
            transcript_part(&path, 0, CHUNK + 1).is_err(),
            "one upload never allocates beyond the chunk cap"
        );
        assert!(
            transcript_part(&path, 16, 0).is_err(),
            "invalid ranges are refused before allocation"
        );
    }

    #[test]
    fn snapshot_reads_cwd_and_version_from_the_newest_rows() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        fs::write(&path, "{\"cwd\":\"/old\",\"version\":\"2.1.1\"}\n{\"cwd\":\"/new\",\"version\":\"2.1.283\"}\n{\"type\":\"x\"}\n").unwrap();
        let (cwd, version) = cwd_and_version(&path).unwrap();
        assert_eq!(cwd.as_deref(), Some(Path::new("/new")));
        assert_eq!(version.as_deref(), Some("2.1.283"));
    }

    #[test]
    fn snapshot_sidecars_are_deterministic_and_regular_files_only() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("c.jsonl");
        assert!(sidecars(&transcript, "c").unwrap().is_none());
        fs::create_dir_all(dir.path().join("c/subagents")).unwrap();
        fs::write(dir.path().join("c/subagents/a.jsonl"), "x").unwrap();
        std::os::unix::fs::symlink("/etc/passwd", dir.path().join("c/subagents/link")).unwrap();
        let first = sidecars(&transcript, "c").unwrap().unwrap();
        assert_eq!(sidecars(&transcript, "c").unwrap().unwrap(), first);
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(first.0.as_slice()));
        let names: Vec<_> = archive
            .entries()
            .unwrap()
            .map(|e| e.unwrap().path().unwrap().into_owned())
            .collect();
        assert_eq!(names, [PathBuf::from("subagents/a.jsonl")]);
    }

    #[tokio::test]
    async fn snapshot_code_is_unchanged_until_the_tree_moves_and_too_large_is_skipped() {
        let dir = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(dir.path()).unwrap();
        let repo = root.join("r");
        let g = |args: &[&str]| {
            assert!(std::process::Command::new("/usr/bin/git")
                .args([
                    "-c",
                    "user.name=T",
                    "-c",
                    "user.email=t@example.invalid",
                    "-c",
                    "commit.gpgsign=false"
                ])
                .args(args)
                .current_dir(&repo)
                .status()
                .unwrap()
                .success())
        };
        fs::create_dir(&repo).unwrap();
        g(&["init", "-q"]);
        let work = root.join("w");
        fs::create_dir(&work).unwrap();
        let sid = "7d3f2a1e-1111-4222-8333-944455556666";
        assert!(matches!(
            capture_code(&repo, sid, &work, None).await.unwrap(),
            CodeOutcome::None
        ));
        g(&["remote", "add", "origin", "https://github.com/o/r.git"]);
        fs::write(repo.join("a"), "1").unwrap();
        g(&["add", "a"]);
        g(&["commit", "-qm", "c"]);
        // A base advertised only by upstream must travel in the bundle: the
        // receiving Mac fetches origin only.
        g(&["update-ref", "refs/remotes/upstream/main", "HEAD"]);
        let CodeOutcome::Captured(first) = capture_code(&repo, sid, &work, None).await.unwrap()
        else {
            panic!("expected a capture");
        };
        let received = work.join("received.bundle");
        fs::write(&received, &first.bytes).unwrap();
        let receiver = root.join("receiver");
        fs::create_dir(&receiver).unwrap();
        git(&receiver, &["init", "-q"], None).await.unwrap();
        git(
            &receiver,
            &["bundle", "verify", &received.to_string_lossy()],
            None,
        )
        .await
        .unwrap();
        assert!(matches!(
            capture_code(&repo, sid, &work, Some(&first.key))
                .await
                .unwrap(),
            CodeOutcome::Unchanged
        ));
        fs::write(repo.join("a"), "2").unwrap();
        assert!(matches!(
            capture_code(&repo, sid, &work, Some(&first.key))
                .await
                .unwrap(),
            CodeOutcome::Captured(_)
        ));
        let big = fs::File::create(repo.join("big.bin")).unwrap();
        big.set_len(UNTRACKED_MAX + 1).unwrap();
        assert!(matches!(
            capture_code(&repo, sid, &work, None).await.unwrap(),
            CodeOutcome::TooLarge
        ));
    }

    #[test]
    fn snapshot_nothing_to_do_is_decided_from_cheap_facts() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("c.jsonl");
        let empty = sidecar_signature(&transcript, "c").unwrap();
        fs::create_dir_all(dir.path().join("c/tool-results")).unwrap();
        fs::write(dir.path().join("c/tool-results/r.txt"), "x").unwrap();
        let one = sidecar_signature(&transcript, "c").unwrap();
        assert_ne!(one, empty);
        assert_eq!(
            sidecar_signature(&transcript, "c").unwrap(),
            one,
            "stable while untouched"
        );
        fs::write(dir.path().join("c/tool-results/r.txt"), "xy").unwrap();
        assert_ne!(
            sidecar_signature(&transcript, "c").unwrap(),
            one,
            "a size change moves it"
        );

        let local = Local {
            seen_len: 10,
            sidecars_sig: Some(one.clone()),
            ..Local::fresh(1)
        };
        assert!(quiet(false, &local, 10, &one, false, false));
        assert!(!quiet(true, &local, 10, &one, false, false), "no state yet");
        assert!(
            !quiet(false, &local, 11, &one, false, false),
            "transcript grew"
        );
        assert!(
            !quiet(false, &local, 10, &empty, false, false),
            "sidecars moved"
        );
        assert!(
            !quiet(false, &local, 10, &one, true, false),
            "code check due"
        );
        assert!(
            !quiet(false, &local, 10, &one, false, true),
            "ended forces a final snapshot"
        );
    }

    #[test]
    fn snapshot_code_turning_too_large_is_a_change_once() {
        let committed = Local {
            code_key: Some("head:tree".into()),
            ..Local::fresh(1)
        };
        assert!(
            moved(&committed, 0, &CodeOutcome::TooLarge, &Side::Same),
            "the committed bundle must be dropped"
        );
        assert!(
            !moved(&Local::fresh(1), 0, &CodeOutcome::TooLarge, &Side::Same),
            "already dropped: nothing to say"
        );
        assert!(!moved(&committed, 0, &CodeOutcome::Unchanged, &Side::Same));
    }

    #[test]
    fn snapshot_repository_gone_drops_the_bundle_once() {
        let committed = Local {
            code_key: Some("head:tree".into()),
            ..Local::fresh(1)
        };
        assert!(
            moved(&committed, 0, &CodeOutcome::None, &Side::Same),
            "no origin or HEAD any more: the committed bundle must be dropped"
        );
        assert!(!moved(&Local::fresh(1), 0, &CodeOutcome::None, &Side::Same));
        assert_eq!(
            Value::Object(skipped(&CodeOutcome::None, &Side::Same)),
            json!({"codeSkipped": "none"})
        );
        assert_eq!(
            Value::Object(skipped(&CodeOutcome::TooLarge, &Side::Same)),
            json!({"codeSkipped": "too-large"})
        );
        assert!(skipped(&CodeOutcome::Unchanged, &Side::Same).is_empty());
    }

    #[test]
    fn snapshot_sidecars_all_deleted_is_a_change() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("c.jsonl");
        fs::create_dir_all(dir.path().join("c/subagents")).unwrap();
        fs::write(dir.path().join("c/subagents/a.jsonl"), "x").unwrap();
        let built = sidecars(&transcript, "c").unwrap();
        let committed = Local {
            sidecars_sha: built.as_ref().map(|(_, d)| d.clone()),
            ..Local::fresh(1)
        };
        assert_eq!(side_change(&committed, built), Side::Same);
        fs::remove_file(dir.path().join("c/subagents/a.jsonl")).unwrap();
        let gone = side_change(&committed, sidecars(&transcript, "c").unwrap());
        assert_eq!(gone, Side::Gone);
        assert!(
            moved(&committed, 0, &CodeOutcome::Unchanged, &gone),
            "a change within the series, not a new one"
        );
        assert_eq!(
            Value::Object(skipped(&CodeOutcome::Unchanged, &gone)),
            json!({"sidecarsSkipped": "none"})
        );
        assert_eq!(
            side_change(&Local::fresh(1), None),
            Side::Same,
            "never had any"
        );
    }

    #[test]
    fn snapshot_sidecars_over_the_cap_are_left_out_not_a_failure() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("c.jsonl");
        fs::create_dir_all(dir.path().join("c/tool-results")).unwrap();
        fs::write(dir.path().join("c/tool-results/r.txt"), "x").unwrap();
        let committed = Local {
            sidecars_sha: sidecars(&transcript, "c").unwrap().map(|(_, d)| d),
            ..Local::fresh(1)
        };
        let big = fs::File::create(dir.path().join("c/tool-results/big.bin")).unwrap();
        big.set_len(SIDECARS_MAX + 1).unwrap();
        let built =
            sidecars(&transcript, "c").expect("an oversized archive must not abort the pass");
        let side = side_change(&committed, built);
        assert_eq!(side, Side::Gone, "the committed archive is dropped");
        assert_eq!(
            Value::Object(skipped(&CodeOutcome::Unchanged, &side)),
            json!({"sidecarsSkipped": "none"})
        );
    }

    #[test]
    fn snapshot_record_longer_than_a_part_is_split_across_parts() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.jsonl");
        let mut content = b"{\"a\":1}\n".to_vec();
        content.extend(std::iter::repeat_n(b'x', 2 * CHUNK as usize + 10));
        content.extend(b"\n{\"b\":2}\n");
        fs::write(&path, &content).unwrap();
        let parts = transcript_parts(&path, 0, content.len() as u64).unwrap();
        assert!(parts.len() >= 3, "the long record spans parts");
        let mut raw = vec![];
        let mut next = 0;
        for (from, to, size, digest) in &parts {
            let gz = transcript_part(&path, *from, *to).unwrap();
            assert_eq!(*size, gz.len());
            assert_eq!(*digest, sha(&gz));
            assert_eq!(*from, next, "contiguous");
            assert!(to - from <= CHUNK, "no part exceeds a chunk");
            next = *to;
            flate2::read::GzDecoder::new(gz.as_slice())
                .read_to_end(&mut raw)
                .unwrap();
        }
        assert_eq!(raw, content, "concatenated parts are the transcript");
    }

    #[test]
    fn snapshot_sidecar_archive_over_the_cap_is_left_out() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("c.jsonl");
        fs::create_dir_all(dir.path().join("c/tool-results")).unwrap();
        // Incompressible bytes exactly at the cap: raw sizes pass, the archive does not.
        let noise: Vec<u8> = (0u32..32)
            .flat_map(|i| Sha256::digest(i.to_le_bytes()))
            .collect();
        fs::write(dir.path().join("c/tool-results/r.bin"), &noise).unwrap();
        let max = noise.len() as u64;
        assert!(sidecars_within(&transcript, "c", max).unwrap().is_none());
        assert!(sidecars_within(&transcript, "c", 64 * 1024)
            .unwrap()
            .is_some());
    }

    #[test]
    fn snapshot_offer_tags_busy_and_ended() {
        let entries = json!([
            {"sessionId":"7d3f2a1e-1111-4222-8333-944455556666","provider":"claude","conversationId":"8d3f2a1e-1111-4222-8333-944455556666"},
            {"sessionId":"not-a-uuid","provider":"claude","conversationId":"8d3f2a1e-1111-4222-8333-944455556666"},
            {"sessionId":"9d3f2a1e-1111-4222-8333-944455556666","provider":"codex","conversationId":"8d3f2a1e-1111-4222-8333-944455556666"}
        ]);
        assert!(
            tag(&Value::Null, &BTreeMap::new()).is_empty(),
            "older backend: no key"
        );
        let list = tag(&entries, &BTreeMap::new());
        assert_eq!(
            list.len(),
            1,
            "invalid ids and non-claude providers are dropped"
        );
        assert!(!list[0].busy && list[0].ended, "no mirror: not busy, ended");
    }

    #[tokio::test]
    async fn no_transcript_waits_for_the_first_turn_and_is_missing_only_once_ended() {
        let dir = tempfile::tempdir().unwrap();
        // No key: any report fails before it leaves, so Ok(None) proves nothing was said.
        let author = Author { dir: dir.path().into(), api: "http://127.0.0.1:9".into(), key_pem: String::new(), device: "d".into(), owner: "o".into() };
        let mut wanted = Wanted { session_id: "7d3f2a1e-1111-4222-8333-944455556666".into(),
            conversation_id: uuid::Uuid::new_v4().to_string(), busy: false, ended: false };
        let client = reqwest::Client::new();
        assert_eq!(snapshot_once(&author, &client, &wanted, false, &mut HashMap::new()).await, Ok(None), "not written yet: ask again later");
        wanted.ended = true;
        assert!(snapshot_once(&author, &client, &wanted, false, &mut HashMap::new()).await.is_err(), "ended with none: reported missing");
    }
}
